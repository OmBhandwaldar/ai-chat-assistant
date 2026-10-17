"use client";

import {
  ChevronsLeft,
  ChevronsRight,
  ChevronDown,
  Search,
  SquarePen,
  AudioLines,
  ImagePlus,
  Plus,
} from "lucide-react";
import { useAppStore } from "@/stores/app-store";
import { cn } from "@/lib/utils";
import {
  Avatar,
  AvatarFallback,
} from "@/components/ui/avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const navItems = [
  { icon: Search, label: "Search", disabled: true },
  { icon: SquarePen, label: "Chat", disabled: false }, // starts a new chat
  { icon: AudioLines, label: "Voice", disabled: true },
  { icon: ImagePlus, label: "Imagine", disabled: true },
];

function initials(name: string) {
  return name
    .split(" ")
    .map((p) => p[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

export function Sidebar() {
  const collapsed = useAppStore((s) => s.sidebarCollapsed);
  const toggle = useAppStore((s) => s.toggleSidebar);
  const users = useAppStore((s) => s.users);
  const currentUser = useAppStore((s) => s.currentUser);
  const setCurrentUser = useAppStore((s) => s.setCurrentUser);
  const conversations = useAppStore((s) => s.conversations);
  const activeConversationId = useAppStore((s) => s.activeConversationId);
  const startNewConversation = useAppStore((s) => s.startNewConversation);
  const openConversation = useAppStore((s) => s.openConversation);

  if (collapsed) {
    return (
      <aside className="flex h-full w-14 flex-col items-center gap-4 border-r border-neutral-200 bg-white py-4">
        <button
          onClick={toggle}
          className="rounded-md p-1.5 text-neutral-500 hover:bg-neutral-100"
          aria-label="Expand sidebar"
        >
          <ChevronsRight size={18} />
        </button>
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-neutral-900 text-white text-xs font-semibold">
          AI
        </div>
      </aside>
    );
  }

  return (
    <aside className="flex h-full w-64 flex-col border-r border-neutral-200 bg-white">
      {/* Logo + collapse */}
      <div className="flex items-center justify-between px-4 py-4">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-neutral-900 text-white text-xs font-semibold">
            AI
          </div>
          <span className="text-sm font-semibold text-neutral-900">
            Asset Copilot
          </span>
        </div>
        <button
          onClick={toggle}
          className="rounded-md p-1.5 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-700"
          aria-label="Collapse sidebar"
        >
          <ChevronsLeft size={16} />
        </button>
      </div>

      {/* Nav */}
      <nav className="flex flex-col gap-0.5 px-2">
        {navItems.map(({ icon: Icon, label, disabled }) => (
          <button
            key={label}
            disabled={disabled}
            onClick={label === "Chat" ? startNewConversation : undefined}
            className={cn(
              "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium text-left",
              disabled
                ? "text-neutral-300 cursor-not-allowed"
                : "text-neutral-900 hover:bg-neutral-100"
            )}
          >
            <Icon size={17} strokeWidth={2} />
            {label}
          </button>
        ))}
      </nav>

      <div className="mt-5 px-2">
        <div className="flex items-center justify-between px-3 py-1.5">
          <button className="flex items-center gap-1 text-sm font-semibold text-neutral-900">
            Projects <ChevronDown size={14} className="text-neutral-400" />
          </button>
        </div>
        <button className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-neutral-500 hover:bg-neutral-100">
          <Plus size={16} />
          New Project
        </button>
      </div>

      <div className="mt-5 flex-1 overflow-y-auto px-2">
        <div className="flex items-center justify-between px-3 py-1.5">
          <button className="flex items-center gap-1 text-sm font-semibold text-neutral-900">
            History <ChevronDown size={14} className="text-neutral-400" />
          </button>
        </div>
        <div className="flex flex-col gap-0.5">
          {/* A chat that hasn't been sent yet isn't persisted, so show it as a draft row. */}
          {activeConversationId === null && (
            <div className="rounded-lg bg-neutral-100 px-3 py-2 text-sm text-neutral-700">
              New conversation
            </div>
          )}
          {conversations.map((c) => (
            <button
              key={c.conversation_id}
              onClick={() => openConversation(c.conversation_id)}
              className={cn(
                "truncate rounded-lg px-3 py-2 text-left text-sm text-neutral-700 hover:bg-neutral-100",
                c.conversation_id === activeConversationId && "bg-neutral-100 font-medium"
              )}
            >
              {c.title}
            </button>
          ))}
          <button className="px-3 py-1.5 text-left text-sm text-neutral-400 hover:text-neutral-600">
            See all
          </button>
        </div>
      </div>

      {/* User picker */}
      <div className="border-t border-neutral-200 p-3">
        <DropdownMenu>
          <DropdownMenuTrigger className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-neutral-100">
            <Avatar className="h-7 w-7">
              <AvatarFallback className="bg-neutral-900 text-[11px] text-white">
                {currentUser ? initials(currentUser.name) : "?"}
              </AvatarFallback>
            </Avatar>
            <div className="flex-1 text-left">
              <div className="text-sm font-medium text-neutral-900 truncate">
                {currentUser?.name ?? "Select user"}
              </div>
              {currentUser && (
                <div className="text-xs text-neutral-400 truncate">
                  {currentUser.role.replace(/_/g, " ")}
                </div>
              )}
            </div>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-56">
            {users.map((u) => (
              <DropdownMenuItem key={u.user_id} onClick={() => setCurrentUser(u)}>
                <Avatar className="h-6 w-6">
                  <AvatarFallback className="bg-neutral-900 text-[10px] text-white">
                    {initials(u.name)}
                  </AvatarFallback>
                </Avatar>
                <div className="flex flex-col">
                  <span className="text-sm">{u.name}</span>
                  <span className="text-xs text-neutral-400">
                    {u.role.replace(/_/g, " ")}
                  </span>
                </div>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </aside>
  );
}

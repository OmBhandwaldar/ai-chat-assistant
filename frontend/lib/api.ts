import type { AppUser, ConversationSummary, Evidence, Conflict, UIVisual } from "@/lib/types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export async function fetchUsers(): Promise<AppUser[]> {
  const res = await fetch(`${API_BASE}/users`);
  if (!res.ok) throw new Error(`GET /users failed: ${res.status}`);
  return res.json();
}

export async function fetchConversations(
  userId: string
): Promise<ConversationSummary[]> {
  const res = await fetch(
    `${API_BASE}/conversations?user_id=${encodeURIComponent(userId)}`
  );
  if (!res.ok) throw new Error(`GET /conversations failed: ${res.status}`);
  return res.json();
}

export interface StoredMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  ts: string;
}

/** Past turns of one conversation. Scoped to `userId` server-side (history is private). */
export async function fetchConversationMessages(
  conversationId: string,
  userId: string
): Promise<StoredMessage[]> {
  const res = await fetch(
    `${API_BASE}/conversations/${encodeURIComponent(conversationId)}/messages?user_id=${encodeURIComponent(userId)}`
  );
  if (!res.ok) throw new Error(`GET /conversations/:id/messages failed: ${res.status}`);
  return res.json();
}

export interface AgentAnswerPayload {
  text: string;
  visual: UIVisual;
  citations: Evidence[];
  conflicts: Conflict[];
}

interface WireMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

interface StreamHandlers {
  /** Called as plain assistant text streams in, before the final structured answer arrives. */
  onTextDelta?: (delta: string) => void;
  /** Called once with the full structured AgentAnswer (text, visual, citations, conflicts). */
  onAnswer: (answer: AgentAnswerPayload) => void;
  onError?: (message: string) => void;
}

/**
 * POST the conversation so far to /ag-ui and stream back AG-UI protocol SSE events.
 * The agent's output is a structured Pydantic model (AgentAnswer), not plain text, so the
 * backend ships it as a single CUSTOM event (name="agent_answer") rather than relying on
 * TEXT_MESSAGE_* deltas alone.
 */
export async function streamAgentRun(
  params: {
    userId: string;
    threadId: string;
    runId: string;
    messages: WireMessage[];
    /** Aborting stops the stream silently (no onError) — used by the Stop button. */
    signal?: AbortSignal;
  },
  handlers: StreamHandlers
): Promise<void> {
  const { userId, threadId, runId, messages, signal } = params;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/ag-ui`, {
      signal,
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-User-Id": userId,
      },
      body: JSON.stringify({
        threadId,
        runId,
        messages,
        tools: [],
        context: [],
        forwardedProps: {},
      }),
    });
  } catch {
    if (!signal?.aborted) handlers.onError?.("Could not reach the agent service.");
    return;
  }

  if (!res.ok || !res.body) {
    // FastAPI error bodies look like {"detail": "..."} — surface that when present.
    let detail = "";
    try {
      detail = String((await res.json())?.detail ?? "");
    } catch {
      // body wasn't JSON; fall back to the status code alone
    }
    handlers.onError?.(
      detail ? `Agent request failed (${res.status}): ${detail}` : `Agent request failed (${res.status}).`
    );
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const consume = (chunk: string) => {
    const dataLine = chunk.split("\n").find((l) => l.startsWith("data:"));
    if (!dataLine) return;
    const raw = dataLine.slice("data:".length).trim();
    if (!raw) return;

    let event: Record<string, unknown>;
    try {
      event = JSON.parse(raw);
    } catch {
      return;
    }

    switch (event.type) {
      case "TEXT_MESSAGE_CONTENT":
        handlers.onTextDelta?.(String(event.delta ?? ""));
        break;
      case "CUSTOM":
        if (event.name === "agent_answer") {
          handlers.onAnswer(event.value as AgentAnswerPayload);
        }
        break;
      case "RUN_ERROR":
        handlers.onError?.(String(event.message ?? "Agent run failed."));
        break;
      default:
        break;
    }
  };

  while (true) {
    let readResult: ReadableStreamReadResult<Uint8Array>;
    try {
      readResult = await reader.read();
    } catch {
      if (!signal?.aborted) handlers.onError?.("The connection to the agent was interrupted.");
      return;
    }
    const { done, value } = readResult;
    if (done) {
      buffer += decoder.decode();
      if (buffer.trim()) consume(buffer);
      break;
    }
    buffer += decoder.decode(value, { stream: true });

    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) consume(chunk);
  }
}

// Minimal Server-Sent Events reader for a fetch() body (EventSource cannot POST).

export interface SSEEvent {
  event: string;
  data: unknown;
}

/** Parse complete events out of `buffer`; returns them and the unconsumed remainder. */
export function parseSSE(buffer: string): { events: SSEEvent[]; rest: string } {
  const events: SSEEvent[] = [];
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (data.length) {
      try {
        events.push({ event, data: JSON.parse(data.join("\n")) });
      } catch {
        events.push({ event, data: data.join("\n") });
      }
    }
  }
  return { events, rest };
}

export async function* readSSE(response: Response, signal?: AbortSignal): AsyncGenerator<SSEEvent> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal?.aborted) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      const parsed = parseSSE(buffer);
      buffer = parsed.rest;
      yield* parsed.events;
    }
  } finally {
    reader.releaseLock();
  }
}

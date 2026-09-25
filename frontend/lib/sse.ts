import { API_BASE, API_V1 } from "./api";

export interface AgentEvent {
  type: string;
  data: Record<string, unknown>;
  /** Position in the run's durable timeline. Absent only on client-made events. */
  sequence?: number;
}

/**
 * Follow a run's durable timeline.
 *
 * The important property, and the reason this is not `EventSource`: the server
 * is not producing these events for us, it is *reading them from storage*.
 * So a dropped connection loses nothing. We remember the last sequence we saw
 * and ask for everything after it — on reconnect, on a tab returning from the
 * background, on a laptop waking from sleep.
 *
 * `EventSource` would give us automatic reconnection but cannot send cookies
 * cross-origin or set headers, so reconnection is handled here instead, with
 * the same `after` cursor semantics the server exposes.
 */
export async function followRun(
  runId: string,
  onEvent: (event: AgentEvent) => void,
  options: { after?: number; signal?: AbortSignal; maxAttempts?: number } = {},
): Promise<{ lastSequence: number; ended: boolean }> {
  let cursor = options.after ?? 0;
  const maxAttempts = options.maxAttempts ?? 8;
  let attempt = 0;

  for (;;) {
    if (options.signal?.aborted) return { lastSequence: cursor, ended: false };

    let ended = false;
    try {
      const result = await readStream(
        `${API_V1}/runs/${runId}/events?after=${cursor}`,
        (event) => {
          if (typeof event.sequence === "number") cursor = event.sequence;
          if (event.type === "stream.end") {
            ended = true;
            return;
          }
          onEvent(event);
        },
        options.signal,
      );
      if (result.fatal) {
        // 404 / 403 — retrying cannot help and would spin.
        onEvent({ type: "error", data: { message: result.message } });
        return { lastSequence: cursor, ended: true };
      }
      if (ended) return { lastSequence: cursor, ended: true };
      attempt = 0;
    } catch (err) {
      if (options.signal?.aborted) return { lastSequence: cursor, ended: false };
      attempt += 1;
      if (attempt >= maxAttempts) {
        onEvent({
          type: "error",
          data: {
            message:
              "Lost the connection to this run. The work is still going on the " +
              "server — reload to catch up.",
          },
        });
        return { lastSequence: cursor, ended: false };
      }
      await sleep(Math.min(8000, 250 * 2 ** attempt));
    }
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function readStream(
  path: string,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<{ fatal: boolean; message: string }> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: { Accept: "text/event-stream" },
    signal,
  });

  if (!res.ok || !res.body) {
    let message = `Could not follow this run (${res.status})`;
    try {
      const detail = await res.json();
      if (detail?.detail) message = String(detail.detail);
    } catch {
      /* keep default message */
    }
    // 5xx is worth retrying; a 4xx means this run is not ours to watch.
    return { fatal: res.status < 500, message };
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const event = parseChunk(chunk);
      if (event) onEvent(event);
      boundary = buffer.indexOf("\n\n");
    }
  }
  return { fatal: false, message: "" };
}

function parseChunk(chunk: string): AgentEvent | null {
  let type = "message";
  let sequence: number | undefined;
  const dataLines: string[] = [];
  for (const line of chunk.split("\n")) {
    if (line.startsWith(":")) continue; // keep-alive comment
    if (line.startsWith("id:")) {
      const parsed = Number.parseInt(line.slice(3).trim(), 10);
      if (!Number.isNaN(parsed)) sequence = parsed;
    } else if (line.startsWith("event:")) {
      type = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (!dataLines.length) return null;
  try {
    return { type, data: JSON.parse(dataLines.join("\n")), sequence };
  } catch {
    return { type, data: { raw: dataLines.join("\n") }, sequence };
  }
}

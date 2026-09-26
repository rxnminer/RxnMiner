import type { RuntimeEvent, SessionStatus } from "./contracts.js";
import { RuntimePolicyError } from "./errors.js";

export function parseEventCursor(queryAfter: string | null, lastEventId: string | undefined): number {
  const values = [queryAfter ?? "0", lastEventId ?? "0"];
  const parsed = values.map((value) => {
    if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value))) {
      throw new RuntimePolicyError("INVALID_CURSOR", "event cursor must be a non-negative integer", 400);
    }
    return Number(value);
  });
  return Math.max(...parsed);
}

export function encodeSseEvent(event: RuntimeEvent): string {
  return `id: ${event.seq}\ndata: ${JSON.stringify(event)}\n\n`;
}

export function terminalSession(status: SessionStatus): boolean {
  return [
    "blocked",
    "dormant",
    "failed",
    "aborted",
    "review_ready",
    "partial",
    "cancelled",
    "interrupted",
  ].includes(status);
}

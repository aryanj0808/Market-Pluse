import type { Sourced } from "../types";

export type Freshness = { status: "fresh" | "stale" | "missing" | "silent"; age: number | null; label: string };

const SILENT_AFTER_MS = 10_000; // no message from the gateway for this long: the feed itself is down

/** Freshness of one sourced section: stale if the server said so, if its age exceeds its TTL on
 *  the (extrapolated) server clock, or if the stream has gone silent. */
export function freshness(
  section: Pick<Sourced<unknown>, "as_of" | "ttl_seconds" | "stale"> | null | undefined,
  serverNowMs: number | null,
  sinceLastMessageMs: number,
): Freshness {
  if (!section) return { status: "missing", age: null, label: "no data" };
  const age = serverNowMs === null ? null : Math.max(0, (serverNowMs - Date.parse(section.as_of)) / 1000);
  if (sinceLastMessageMs > SILENT_AFTER_MS)
    return { status: "silent", age, label: `no updates for ${Math.round(sinceLastMessageMs / 1000)}s` };
  const stale = section.stale || (age !== null && age > section.ttl_seconds);
  return { status: stale ? "stale" : "fresh", age, label: age === null ? "" : formatAge(age) };
}

export function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

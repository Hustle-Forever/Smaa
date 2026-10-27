export function fmtRel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function fmtDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${(seconds % 60).toFixed(0)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

export function durationBetween(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
): string {
  if (!startIso) return "—";
  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  return fmtDuration(Math.max(0, (end - start) / 1000));
}

export function fmtBytes(b: number | null | undefined): string {
  if (b == null) return "—";
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 * 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  return `${(b / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}

export function fmtNumber(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("en-US").format(n);
}

const AGENT_LABELS: Record<string, string> = {
  "website-ingest": "Website crawl",
  "brand-doc-ingest": "Brand-doc ingest",
  "brand-synthesizer": "Re-synthesize",
  "instagram-ingest": "Instagram ingest",
};

export function agentLabel(id: string): string {
  return AGENT_LABELS[id] ?? id;
}

/**
 * The message a person should read, out of the error `http()` threw.
 *
 * `http()` throws `Error("502 <detail>")` — the status code is for a log, not
 * for somebody looking at a screen, and it turns a sentence written for them
 * into something that reads like a stack trace. The detail itself is already
 * plain language: the backend's services/gen_errors.py makes sure of it.
 *
 * Lives here rather than in a feature folder because every surface that calls
 * the API needs it. `components/trends/trendsApi.ts` re-exports this one so
 * there is a single implementation.
 */
/**
 * The bare HTTP reason phrases, which are not sentences and not for readers.
 *
 * The assumption above holds only while the backend answers. When it does not,
 * what arrives is the status line and nothing else, so `http()` throws
 * `Error("404 Not Found")` and the reader gets the words "Not Found" in a red
 * box: two capitalised words that name no subject, propose no action, and read
 * like a fragment of a log. Keyed by the whole phrase, so a real backend detail
 * that merely happens to contain one of these words is untouched.
 */
const BARE_STATUS_TEXT: Record<string, string> = {
  "not found": "That address is not on the server. It may have moved.",
  "internal server error": "The server hit an error. Try again in a moment.",
  "bad gateway": "The server did not answer. It may be restarting.",
  "service unavailable": "The server is not accepting requests right now.",
  "gateway timeout": "The server took too long to answer.",
  unauthorized: "You are not signed in for this.",
  forbidden: "This account cannot do that.",
  "bad request": "The server refused that request.",
  "request timeout": "That took too long and was given up on.",
  "too many requests": "Too many requests at once. Wait a moment and retry.",
  "failed to fetch": "Could not reach the server. Check that it is running.",
  "load failed": "Could not reach the server. Check that it is running.",
  "network error": "Could not reach the server. Check that it is running.",
};

export function humanError(e: unknown, fallback = "Something went wrong"): string {
  const msg = e instanceof Error ? e.message : String(e ?? "");
  const stripped = msg.replace(/^\d{3}\s+/, "").trim();
  if (!stripped) return fallback;
  return BARE_STATUS_TEXT[stripped.toLowerCase()] ?? stripped;
}

/**
 * The clock half of every timestamp on the platform, in one place.
 *
 * `hour12` is passed EXPLICITLY rather than left to the locale. Every call
 * site formats with `undefined` as its locale, which means the browser's, and
 * the browser's is 24-hour for most of Europe and the Gulf: the same scheduled
 * post read "14:17" on one machine and "2:17 PM" on the next, off the same
 * row. A reading that changes shape with who is looking is not a format.
 *
 * `hour: "numeric"` and not `"2-digit"`, because a padded hour is a 24-hour
 * habit: "02:17 PM" reads as a military timestamp somebody bolted a meridiem
 * onto. 12-hour clocks do not pad.
 *
 * Spread it into the options object rather than replacing it. Each call site
 * still chooses how much of the DATE it prints (a queue row wants a weekday, a
 * detail view wants a year); only the clock is fixed here.
 */
export const CLOCK_12H = {
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
} as const satisfies Intl.DateTimeFormatOptions;

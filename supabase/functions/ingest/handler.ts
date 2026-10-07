// Request handling for the ingest endpoint, kept free of Supabase imports so it can be tested with a fake store.
// Auth: `Authorization: Bearer <per-dev ingest token>`. The token maps to exactly one dev row,
// so the dev identity is stamped server-side and a client cannot report events as someone else.

export const MAX_BATCH = 500;
const ID_RE = /^[0-9a-f]{40}$/;
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

export type Row = Record<string, unknown>;

export interface Store {
  /** The dev holding this token hash, or null. */
  findDev(tokenHash: string): Promise<{ id: string; revoked_at: string | null } | null>;
  /** Insert rows, ignoring ids that already exist. Returns an error message or null. */
  insertIgnoringDuplicates(rows: Row[]): Promise<string | null>;
}

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function clip(value: unknown, max: number): string | null {
  return typeof value === "string" && value.length > 0 ? value.slice(0, max) : null;
}

export function toRow(value: unknown, devId: string): Row | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const ev = value as Row;
  const ts = typeof ev.ts === "string" ? Date.parse(ev.ts) : NaN;
  if (typeof ev.id !== "string" || !ID_RE.test(ev.id)) return null;
  if (Number.isNaN(ts) || ts > Date.now() + 5 * 60_000) return null;
  if (typeof ev.plugin !== "string" || !SLUG_RE.test(ev.plugin)) return null;
  if (typeof ev.skill !== "string" || !SLUG_RE.test(ev.skill)) return null;
  if (ev.trigger !== "slash" && ev.trigger !== "model") return null;
  return {
    id: ev.id,
    dev_id: devId,
    ts: new Date(ts).toISOString(),
    session_id: clip(ev.session, 64),
    repo: clip(ev.repo, 128),
    branch: clip(ev.branch, 256),
    cc_version: clip(ev.cc_version, 32),
    plugin: ev.plugin,
    plugin_version: clip(ev.plugin_version, 32),
    skill: ev.skill,
    trigger: ev.trigger,
  };
}

export async function handle(req: Request, store: Store): Promise<Response> {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });

  const token = req.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (token.length < 32) return new Response("unauthorized", { status: 401 });

  const dev = await store.findDev(await sha256Hex(token));
  if (!dev || dev.revoked_at) return new Response("unauthorized", { status: 401 });

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return new Response("invalid json", { status: 400 });
  }
  // `null`, arrays and primitives are valid JSON too; anything but {events: [...]} is a 400, never a 500.
  const events = typeof body === "object" && body !== null ? (body as { events?: unknown }).events : undefined;
  if (!Array.isArray(events) || events.length > MAX_BATCH) {
    return new Response(`events must be an array of at most ${MAX_BATCH}`, { status: 400 });
  }

  const rows = events.map((ev) => toRow(ev, dev.id)).filter((r): r is Row => r !== null);
  if (rows.length > 0) {
    // Re-sent events (retries, backfill overlap) are a no-op, never an update.
    const error = await store.insertIgnoringDuplicates(rows);
    if (error) return new Response("storage error", { status: 500 });
  }

  return Response.json({ accepted: rows.length, rejected: events.length - rows.length });
}

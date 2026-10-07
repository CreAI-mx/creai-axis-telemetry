// Ingest endpoint for creai-axis usage events.
// Auth: `Authorization: Bearer <per-dev ingest token>`. The token maps to exactly one dev row,
// so the dev identity is stamped server-side and a client cannot report events as someone else.
import { createClient } from "jsr:@supabase/supabase-js@2";

const MAX_BATCH = 500;
const ID_RE = /^[0-9a-f]{40}$/;
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function clip(value: unknown, max: number): string | null {
  return typeof value === "string" && value.length > 0 ? value.slice(0, max) : null;
}

type Row = Record<string, unknown>;

function toRow(ev: Row, devId: string): Row | null {
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

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });

  const token = req.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ?? "";
  if (token.length < 32) return new Response("unauthorized", { status: 401 });

  const { data: dev } = await supabase
    .from("axis_usage_devs")
    .select("id, revoked_at")
    .eq("token_hash", await sha256Hex(token))
    .maybeSingle();
  if (!dev || dev.revoked_at) return new Response("unauthorized", { status: 401 });

  let body: { events?: unknown };
  try {
    body = await req.json();
  } catch {
    return new Response("invalid json", { status: 400 });
  }
  if (!Array.isArray(body.events) || body.events.length > MAX_BATCH) {
    return new Response(`events must be an array of at most ${MAX_BATCH}`, { status: 400 });
  }

  const rows = body.events.map((ev) => toRow(ev as Row, dev.id)).filter((r): r is Row => r !== null);
  if (rows.length > 0) {
    // ignoreDuplicates: re-sent events (retries, backfill overlap) are a no-op, never an update.
    const { error } = await supabase
      .from("axis_usage_events")
      .upsert(rows, { onConflict: "id", ignoreDuplicates: true });
    if (error) return new Response("storage error", { status: 500 });
  }

  return Response.json({ accepted: rows.length, rejected: body.events.length - rows.length });
});

// Ingest endpoint for creai-axis usage events: wires the handler to Supabase. Logic lives in handler.ts.
import { createClient } from "jsr:@supabase/supabase-js@2";
import { handle, type Store } from "./handler.ts";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

const store: Store = {
  async findDev(tokenHash) {
    const { data } = await supabase
      .from("axis_usage_devs")
      .select("id, revoked_at")
      .eq("token_hash", tokenHash)
      .maybeSingle();
    return data;
  },
  async insertIgnoringDuplicates(rows) {
    const { error } = await supabase
      .from("axis_usage_events")
      .upsert(rows, { onConflict: "id", ignoreDuplicates: true });
    return error ? error.message : null;
  },
};

Deno.serve((req) => handle(req, store));

import { assertEquals } from "jsr:@std/assert@1";
import { handle, MAX_BATCH, type Row, sha256Hex, type Store } from "./handler.ts";

const TOKEN = "t".repeat(40);
const ID = "a".repeat(40);

function fakeStore(opts: { revoked?: boolean; insertError?: string } = {}) {
  const inserted: Row[] = [];
  const store: Store = {
    async findDev(hash) {
      return hash === await sha256Hex(TOKEN) ? { id: "dev-1", revoked_at: opts.revoked ? "2026-10-01" : null } : null;
    },
    async insertIgnoringDuplicates(rows) {
      inserted.push(...rows);
      return opts.insertError ?? null;
    },
  };
  return { store, inserted };
}

function post(body: string, token = TOKEN) {
  return new Request("https://ingest.test/", {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body,
  });
}

const event = (over: Row = {}) => ({
  id: ID, ts: "2026-10-07T10:00:00Z", session: "s1", repo: "agrizar", branch: "feature/DAIL-1-x",
  cc_version: "2.1.290", plugin: "creai-common", plugin_version: "0.15.0", skill: "creai-implement",
  trigger: "slash", ...over,
});

Deno.test("valid events are stamped with the token's dev and stored", async () => {
  const { store, inserted } = fakeStore();
  const res = await handle(post(JSON.stringify({ events: [event()] })), store);
  assertEquals(res.status, 200);
  assertEquals(await res.json(), { accepted: 1, rejected: 0 });
  assertEquals(inserted[0].dev_id, "dev-1");
});

Deno.test("unknown, short and revoked tokens are 401", async () => {
  const body = JSON.stringify({ events: [event()] });
  assertEquals((await handle(post(body, "x".repeat(40)), fakeStore().store)).status, 401);
  assertEquals((await handle(post(body, "short"), fakeStore().store)).status, 401);
  assertEquals((await handle(post(body), fakeStore({ revoked: true }).store)).status, 401);
});

Deno.test("null, primitive and array bodies are 400, not 500", async () => {
  for (const body of ["null", "42", '"x"', "[]", "{}", '{"events": null}', "not json"]) {
    const res = await handle(post(body), fakeStore().store);
    assertEquals(res.status, 400, `body ${body}`);
    await res.body?.cancel();
  }
});

Deno.test("null and malformed elements are rejected individually", async () => {
  const { store, inserted } = fakeStore();
  const events = [null, 7, "x", [], event(), event({ id: "b".repeat(40), plugin: "Bad Slug" }), event({ trigger: "other" })];
  const res = await handle(post(JSON.stringify({ events })), store);
  assertEquals(res.status, 200);
  assertEquals(await res.json(), { accepted: 1, rejected: 6 });
  assertEquals(inserted.length, 1);
});

Deno.test("oversized batches are 400 and storage errors are 500", async () => {
  const big = JSON.stringify({ events: Array.from({ length: MAX_BATCH + 1 }, () => event()) });
  const tooBig = await handle(post(big), fakeStore().store);
  assertEquals(tooBig.status, 400);
  await tooBig.body?.cancel();
  const failed = await handle(post(JSON.stringify({ events: [event()] })), fakeStore({ insertError: "boom" }).store);
  assertEquals(failed.status, 500);
  await failed.body?.cancel();
});

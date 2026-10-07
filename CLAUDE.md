# creai-axis-telemetry: guide for Claude

This repo is an opt-in usage-telemetry system for creai-axis: a one-plugin Claude Code marketplace
(`plugins/creai-telemetry`), a Supabase backend (`supabase/`) and a static dashboard (`dashboard/`).
Start with `openspec/changes/axis-usage-telemetry/design.md`. The event contract table there is the
source of truth.

## Commands
- Collector tests: `python3 -m unittest discover -s tests/collector`
- Ingest function: `deno check supabase/functions/ingest/` and `deno test --no-lock supabase/functions/ingest/`
- Dashboard locally: `python3 -m http.server -d dashboard 8000`

## Rules
- **Privacy is the product.** Never add a field that carries prompts, skill arguments, code, file
  paths beyond the repo name (never the home folder or OS user name), or anything from non-creai-axis plugins. Any new field needs a
  row in the design's event contract and a line in the skill's "What is sent".
- **The hook never hurts a session.** `hook` always exits 0, never prompts, and stays inside the
  10 s hook timeout. It does nothing until opted in.
- **The collector is stdlib-only Python 3.9+**, a single file, so it installs with no dependencies.
- **One contract, four places.** A change to the event shape updates the collector, the ingest
  function's validation, the migration (a new migration file, never an edit to an applied one) and
  the design doc in the same PR.
- **Events are append-only.** The one exception is deleting a dev's rows on request.
- **Tokens never enter a conversation, a log or the repo.** Only `sha256(token)` is stored server-side.
- **No secrets in the repo.** `dashboard/config.js` and `.env*` are git-ignored; the anon key goes
  only in the deployed `config.js`.
- Bump `plugins/creai-telemetry/.claude-plugin/plugin.json` `version` once per PR that changes the plugin.
- Conventional Commits. Dashboard UI copy is in Spanish; code, comments and docs are in English.

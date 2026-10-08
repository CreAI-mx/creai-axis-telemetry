#!/usr/bin/env bash
# Start the whole system on this machine in Docker: the Supabase backend (database, auth, ingest
# function, mail viewer) and the dashboard. Data persists across restarts. See docs/hosting.md.
set -euo pipefail
cd "$(dirname "$0")/.."

supabase start

# The Supabase CLI publishes its ports on every interface unless Docker's default bind address is
# 127.0.0.1, and this stack runs with default keys and passwords. Refuse to run it open to the network.
exposed="$(docker ps --filter "label=com.supabase.cli.project=creai-axis-telemetry" --format '{{.Names}} {{.Ports}}' \
  | grep -E '(0\.0\.0\.0|\[::\]|:::)[0-9]*:' || true)"
if [ -n "$exposed" ]; then
  echo "Stopping: these containers are reachable from the network, not only from this machine:" >&2
  echo "$exposed" >&2
  echo 'Fix: Docker Desktop > Settings > Docker Engine, add "ip": "127.0.0.1", Apply & restart, then run this again (docs/hosting.md).' >&2
  supabase stop
  exit 1
fi

eval "$(supabase status -o env)"
mail_url="${MAILPIT_URL:-${INBUCKET_URL:-}}"

# The anon key is public by design (RLS guards the data); config.js is git-ignored anyway.
cat > dashboard/config.js <<EOF
// Written by scripts/demo-up.sh for the local Docker stack. Not committed.
window.AXIS_USAGE_CONFIG = {
  supabaseUrl: "${API_URL}",
  supabaseAnonKey: "${ANON_KEY}",
  authProvider: "email",
  mailViewerUrl: "${mail_url}",
};
EOF

docker compose -f deploy/docker-compose.yml up -d

cat <<EOF

Up.
  Dashboard        http://localhost:8080
  Ingest endpoint  ${API_URL}/functions/v1/ingest
  Mail (sign-in)   ${mail_url}
  Studio           ${STUDIO_URL:-}
Issue a token:     python3 scripts/axis_admin.py issue <email>@creai.mx "Name" --out <file>
Stop:              scripts/demo-down.sh
EOF

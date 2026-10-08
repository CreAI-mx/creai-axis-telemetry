#!/usr/bin/env bash
# Stop the local Docker deployment. Data is kept for the next scripts/demo-up.sh;
# `supabase stop --no-backup` would delete it.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose -f deploy/docker-compose.yml down
supabase stop

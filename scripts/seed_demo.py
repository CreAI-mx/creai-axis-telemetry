#!/usr/bin/env python3
"""Fill the local Docker stack with fictitious devs and 13 weeks of events, to show the dashboard with data.

  python3 scripts/seed_demo.py           # replace any earlier demo data with a fresh set
  python3 scripts/seed_demo.py --clear   # remove the demo data only

Local stack only (scripts/demo-up.sh): it refuses to run when AXIS_DB_URL points elsewhere. Demo devs
are `demo-dev-<letter>@creai.mx`, named "Dev A (demo)" and so on, so they can't be mistaken for people.
Events go through the real ingest endpoint with per-dev tokens that live only in this process. Real
devs and their events are never touched.
"""
import argparse
import hashlib
import os
import random
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import axis_admin
import smoke_ingest

DEVS = 14
WEEKS = 13
EMAIL = "demo-dev-{}@creai.mx"
PIPELINE = ["creai-refine-task", "creai-design-feature", "creai-estimate", "creai-implement",
            "creai-audit-pr", "creai-create-pr", "creai-archive"]
OTHERS = [("creai-common", "creai-policies"), ("creai-common", "axis-start"), ("creai-common", "axis-close"),
          ("creai-common", "creai-fix"), ("creai-common", "creai-onboarding"), ("creai-frontend", "creai-figma-bridge"),
          ("creai-data", "creai-fabric"), ("creai-data", "creai-sql-explain"), ("creai-backend", "creai-graphify")]
REPOS = ["agrizar", "origo-api", "origo-web", "fabric-lakehouse", "creai-kit"]


def generate(now, rnd):
    """The dashboard's demoData() shape: staggered adoption, two devs who went quiet, two on an old version."""
    devs = []
    for d in range(DEVS):
        letter = chr(ord("a") + d)
        start_week = int(rnd.random() * 11)
        intensity = 0.35 + rnd.random() * 0.65
        finishes = rnd.random()  # how far down the pipeline this dev usually goes
        last_week = 9 + int(rnd.random() * 2) if d in (11, 12) else WEEKS
        version = "0.14.2" if d in (5, 9) else "0.15.0"
        events = []

        def add(ts, repo, branch, plugin, skill, trigger, cc):
            ts = min(ts, now - timedelta(hours=1))
            events.append({"id": hashlib.sha1(f"demo-{letter}-{len(events)}-{ts.isoformat()}".encode()).hexdigest(),
                           "ts": ts.isoformat(timespec="seconds"), "session": f"demo-{letter}-{ts:%Y%m%d}",
                           "repo": repo, "branch": branch, "cc_version": cc, "plugin": plugin,
                           "plugin_version": version, "skill": skill, "trigger": trigger})

        for w in range(start_week, last_week):
            week_start = now - timedelta(weeks=WEEKS - w)
            ramp = min(1, 0.4 + (w - start_week) * 0.15)
            for t in range(round(rnd.random() * 3 * intensity * ramp + 0.3)):
                branch = f"feature/ORG-{100 + d * 40 + w * 3 + t}-demo"
                repo = rnd.choice(REPOS)
                depth = min(2 + int((finishes * 0.6 + rnd.random() * 0.6) * 6), len(PIPELINE))
                start = week_start + timedelta(days=rnd.random() * 5)
                for s in range(1 if rnd.random() < 0.55 else 0, depth):
                    add(start + timedelta(days=s * 0.6), repo, branch, "creai-common", PIPELINE[s],
                        "slash" if rnd.random() < 0.78 else "model", f"2.1.29{int(rnd.random() * 2)}")
            pool = OTHERS if d % 3 == 0 else OTHERS[:6]
            for _ in range(round(rnd.random() * 4 * intensity)):
                plugin, skill = rnd.choice(pool)
                add(week_start + timedelta(days=rnd.random() * 6), rnd.choice(REPOS), "develop", plugin, skill,
                    "slash" if rnd.random() < 0.5 else "model", "2.1.290")
        devs.append({"email": EMAIL.format(letter), "name": f"Dev {letter.upper()} (demo)",
                     "github": f"demo-dev-{letter}", "opted_in_at": now - timedelta(weeks=WEEKS - start_week),
                     "events": events})
    return devs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clear", action="store_true", help="remove the demo devs and their events, then stop")
    ap.add_argument("--seed", type=int, default=42, help="random seed (same seed, same data shape)")
    args = ap.parse_args(argv)
    if os.environ.get("AXIS_DB_URL"):
        sys.exit("seed_demo.py writes fictitious data, so it runs only against the local stack. Unset AXIS_DB_URL.")
    args.db_url = None
    out = subprocess.run(["supabase", "status", "-o", "env"], capture_output=True, text=True, check=True).stdout
    env = {k: v.strip('"') for k, v in (line.split("=", 1) for line in out.splitlines() if "=" in line)}
    endpoint = env["API_URL"] + "/functions/v1/ingest"

    def sql(query, **params):
        return axis_admin.psql(args, query, **params)

    sql("""delete from public.axis_usage_events where dev_id in
             (select id from public.axis_usage_devs where email like 'demo-dev-%@creai.mx');
           delete from public.axis_usage_devs where email like 'demo-dev-%@creai.mx';""")
    if args.clear:
        print("Demo data removed.")
        return 0

    now = datetime.now(timezone.utc).replace(microsecond=0)
    total = 0
    for dev in generate(now, random.Random(args.seed)):
        token = secrets.token_urlsafe(32)
        sql("""insert into public.axis_usage_devs (email, display_name, github_login, token_hash, opted_in_at)
               values (:'email', :'name', :'github', :'hash', :'opted');""",
            email=dev["email"], name=dev["name"], github=dev["github"],
            hash=hashlib.sha256(token.encode()).hexdigest(), opted=dev["opted_in_at"].isoformat())
        events = dev["events"]
        for i in range(0, len(events), 500):
            status, body = smoke_ingest.post(endpoint, token, {"events": events[i:i + 500]})
            if status != 200 or body.get("rejected"):
                sys.exit(f"ingest refused events for {dev['email']}: {status} {body}")
        total += len(events)
    print(f"Seeded {DEVS} demo devs and {total} events over {WEEKS} weeks. Open http://localhost:8080 and sign in.")
    print("Remove them with: python3 scripts/seed_demo.py --clear")
    return 0


if __name__ == "__main__":
    sys.exit(main())

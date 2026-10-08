#!/usr/bin/env python3
"""End-to-end smoke test of a running backend: issues a throwaway dev, exercises the ingest
endpoint and checks what landed in the database, then deletes the dev and its events.

  python3 scripts/smoke_ingest.py [--endpoint URL] [--db-url URL]

Defaults to the local Docker stack (scripts/demo-up.sh), where it also checks what dashboard readers
can see, signing in by magic link through the local mail viewer. Tokens never leave this process.
"""
import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import axis_admin

EMAIL = "smoke-test@creai.mx"
READER = "smoke-reader@creai.mx"
OUTSIDER = "smoke-outsider@example.com"


def post(endpoint, token, body):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw.strip()[:1] in (b"{", b"[") else raw.decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")


def sign_in(env, email):
    """Magic-link sign-in via the local mail viewer (Mailpit). Returns an access token."""
    api, anon, mail = env["API_URL"], env["ANON_KEY"], env["MAILPIT_URL"]
    query = urllib.parse.quote(f"to:{email}")
    http("DELETE", f"{mail}/api/v1/search?query={query}")
    status, body = http("POST", f"{api}/auth/v1/otp", {"email": email, "create_user": True}, {"apikey": anon})
    if status != 200:
        raise RuntimeError(f"sign-in email for {email}: {status} {body}")
    for _ in range(40):
        _, found = http("GET", f"{mail}/api/v1/search?query={query}")
        if isinstance(found, dict) and found.get("messages"):
            break
        time.sleep(0.25)
    else:
        raise RuntimeError(f"no sign-in email for {email}")
    _, message = http("GET", f"{mail}/api/v1/message/{found['messages'][0]['ID']}")
    # The email carries a link to /auth/v1/verify?token=<token hash>; redeem that hash directly.
    token_hash = re.search(r"/auth/v1/verify\?token=([^&\s)]+)", message["Text"]).group(1)
    status, body = http("POST", f"{api}/auth/v1/verify", {"type": "magiclink", "token_hash": token_hash},
                        {"apikey": anon})
    if status != 200:
        raise RuntimeError(f"verify for {email}: {status} {body}")
    return body["access_token"]


def event(**overrides):
    ev = {"id": secrets.token_hex(20), "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "session": "smoke", "repo": "smoke-repo", "branch": "feature/SMOKE-1-test", "cc_version": "0.0.0",
          "plugin": "creai-common", "plugin_version": "0.0.0", "skill": "creai-implement", "trigger": "slash"}
    ev.update(overrides)
    return ev


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--endpoint")
    ap.add_argument("--db-url")
    args = ap.parse_args(argv)
    env = None  # the local stack's URLs and keys; None against any other backend
    if not args.endpoint:
        out = subprocess.run(["supabase", "status", "-o", "env"], capture_output=True, text=True, check=True).stdout
        env = {k: v.strip('"') for k, v in (line.split("=", 1) for line in out.splitlines() if "=" in line)}
        args.endpoint = env["API_URL"] + "/functions/v1/ingest"

    def sql(query, **params):
        return axis_admin.psql(args, query, **params)

    def count(event_id):
        return int(sql("select count(*) from public.axis_usage_events where id = :'id';", id=event_id)[0][0])

    token = secrets.token_urlsafe(32)
    sql("""insert into public.axis_usage_devs (email, display_name, token_hash) values (:'email', 'Smoke Test', :'hash')
           on conflict (email) do update set token_hash = excluded.token_hash, revoked_at = null;""",
        email=EMAIL, hash=hashlib.sha256(token.encode()).hexdigest())
    failures = []

    def check(name, ok, detail=""):
        print(("ok    " if ok else "FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        good = event()
        status, body = post(args.endpoint, token, {"events": [good]})
        check("valid token and event: 200, accepted 1", status == 200 and body == {"accepted": 1, "rejected": 0}, f"{status} {body}")
        check("the event is stored once", count(good["id"]) == 1)

        status, body = post(args.endpoint, token, {"events": [good]})
        check("re-sent event: 200, still stored once", status == 200 and count(good["id"]) == 1, f"{status} {body}")

        bad = event(skill="Not A Slug")
        status, body = post(args.endpoint, token, {"events": [bad]})
        check("bad skill slug: rejected, not stored",
              status == 200 and body == {"accepted": 0, "rejected": 1} and count(bad["id"]) == 0, f"{status} {body}")

        if env:
            rest, anon = env["API_URL"] + "/rest/v1", env["ANON_KEY"]

            def read(path, bearer):
                return http("GET", f"{rest}/{path}", headers={"apikey": anon, "Authorization": f"Bearer {bearer}"})

            path = f"axis_usage_events?select=skill&id=eq.{good['id']}"
            reader = sign_in(env, READER)
            status, rows = read(path, reader)
            check("@creai.mx reader sees events", status == 200 and rows == [{"skill": "creai-implement"}], f"{status} {rows}")
            status, _ = read("axis_usage_devs?select=token_hash", reader)
            check("reader cannot read token hashes", status in (401, 403), str(status))
            status, rows = read(path, sign_in(env, OUTSIDER))
            check("non-@creai.mx account sees nothing", status == 200 and rows == [], f"{status} {rows}")
            status, _ = read(path, anon)
            check("signed-out visitor is refused", status in (401, 403), str(status))

        status, _ = post(args.endpoint, None, {"events": [event()]})
        check("no token: 401", status == 401, str(status))
        status, _ = post(args.endpoint, secrets.token_urlsafe(32), {"events": [event()]})
        check("unknown token: 401", status == 401, str(status))
        status, _ = post(args.endpoint, token, {"events": "nope"})
        check("malformed body: 400", status == 400, str(status))

        sql("update public.axis_usage_devs set revoked_at = now() where email = :'email';", email=EMAIL)
        late = event()
        status, _ = post(args.endpoint, token, {"events": [late]})
        check("revoked token: 401, not stored", status == 401 and count(late["id"]) == 0, str(status))

        rows = sql("select count(*) from cron.job where jobname = 'axis-usage-retention';")
        check("retention job is scheduled", rows == [["1"]], str(rows))
    finally:
        sql("""delete from public.axis_usage_events where dev_id in (select id from public.axis_usage_devs where email = :'email');
               delete from public.axis_usage_devs where email = :'email';
               delete from auth.users where email in (:'reader', :'outsider');""",
            email=EMAIL, reader=READER, outsider=OUTSIDER)

    print(f"\n{'All checks passed' if not failures else f'{len(failures)} check(s) failed'} against {args.endpoint}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

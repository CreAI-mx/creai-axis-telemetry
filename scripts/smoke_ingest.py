#!/usr/bin/env python3
"""End-to-end smoke test of a running backend: issues a throwaway dev, exercises the ingest
endpoint and checks what landed in the database, then deletes the dev and its events. Every run
uses its own throwaway addresses, so runs never touch real devs or each other.

  python3 scripts/smoke_ingest.py                                     # the local Docker stack
  AXIS_DB_URL=postgresql://... python3 scripts/smoke_ingest.py --endpoint https://...   # any other backend

Locally (scripts/demo-up.sh) it also checks what dashboard readers can see, signing in by magic link
through the local mail viewer, and runs the retention purge. Against any other backend it changes only
its own throwaway rows. The endpoint must be https://, or http:// only to this machine, as in the
collector. Tokens never leave this process.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import axis_admin

# Fresh identities per run, so a run only ever touches (and cleans up) what it created itself.
RUN = secrets.token_hex(4)
EMAIL = f"smoke-test-{RUN}@creai.mx"
READER = f"smoke-reader-{RUN}@creai.mx"
OUTSIDER = f"smoke-outsider-{RUN}@example.com"

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 30x comes back as an error instead of carrying the Authorization header elsewhere


def endpoint_allowed(url):
    """The collector's transport rule (usage-collector.py endpoint_allowed): https:// to a named host,
    or plain http:// only to this machine, with no credentials in the URL."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(host)
    return parts.scheme == "http" and host in LOOPBACK_HOSTS and "@" not in parts.netloc


def urlopen(req):
    """Same rules as the collector: never follow redirects, and keep loopback requests off proxies."""
    if not endpoint_allowed(req.full_url):
        raise SystemExit(f"Refusing {req.full_url}: use https://, or http:// only to localhost.")
    loopback = urllib.parse.urlsplit(req.full_url).hostname in LOOPBACK_HOSTS
    proxies = urllib.request.ProxyHandler({} if loopback else None)
    return urllib.request.build_opener(_NoRedirect, proxies).open(req, timeout=10)


def post(endpoint, token, body):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})},
                                 method=method)
    try:
        with urlopen(req) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw.strip()[:1] in (b"{", b"[") else raw.decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")


def sign_in(env, email):
    """Magic-link sign-in via the local mail viewer (Mailpit). Returns an access token."""
    api, anon, mail = env["API_URL"], env["ANON_KEY"], env.get("MAILPIT_URL") or env["INBUCKET_URL"]  # older CLIs name it INBUCKET_URL
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
    args = ap.parse_args(argv)
    args.db_url = None  # only AXIS_DB_URL selects another database: an argument would show its password in `ps`
    # Local mode (sign-in checks, the purge) needs both the local endpoint and the local database, so a
    # remote database never meets local-only steps, and the smoke dev is cleaned up where it was created.
    if bool(args.endpoint) != bool(os.environ.get("AXIS_DB_URL")):
        sys.exit("Give both --endpoint and AXIS_DB_URL for another backend, or neither for the local stack.")
    if args.endpoint and not endpoint_allowed(args.endpoint):
        sys.exit("The endpoint must be an https:// URL (http:// only for localhost).")
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
    failures = []

    def check(name, ok, detail=""):
        print(("ok    " if ok else "FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    try:
        # Inside the try: psql can fail after the insert committed, and the cleanup must still run.
        sql("insert into public.axis_usage_devs (email, display_name, token_hash) values (:'email', 'Smoke Test', :'hash');",
            email=EMAIL, hash=hashlib.sha256(token.encode()).hexdigest())
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

        if env:
            # Run the retention function: an event older than 13 months goes, a recent one stays. It purges
            # every expired row in the database, so it runs only on the local stack, and inside a
            # transaction that is rolled back, so no event that was already there is ever deleted.
            sql("update public.axis_usage_devs set revoked_at = null where email = :'email';", email=EMAIL)
            old = event(ts=(datetime.now(timezone.utc) - timedelta(days=430)).isoformat(timespec="seconds"))
            status, body = post(args.endpoint, token, {"events": [old]})
            rows = sql("""begin;
                          select public.axis_usage_purge_expired();
                          select (select count(*) from public.axis_usage_events where id = :'old'),
                                 (select count(*) from public.axis_usage_events where id = :'good');
                          rollback;""", old=old["id"], good=good["id"])
            purged, inside = rows[0][0], rows[1]  # inside the transaction: [expired left, recent left]
            check("retention purge deletes only expired events (then rolled back)",
                  status == 200 and int(purged) >= 1 and inside == ["0", "1"] and count(old["id"]) == 1,
                  f"{status} {body} purged={purged} inside={inside}")
    finally:
        sql("""delete from public.axis_usage_events where dev_id in (select id from public.axis_usage_devs where email = :'email');
               delete from public.axis_usage_devs where email = :'email';""", email=EMAIL)
        if env:  # sign-in accounts exist only on the local stack (auth.users is Supabase's)
            sql("delete from auth.users where email in (:'reader', :'outsider');", reader=READER, outsider=OUTSIDER)

    print(f"\n{'All checks passed' if not failures else f'{len(failures)} check(s) failed'} against {args.endpoint}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

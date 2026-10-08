#!/usr/bin/env python3
"""Admin tasks for the usage backend: issue, revoke and list ingest tokens; forget a dev's events.

Talks plain SQL through psql, so it works the same against the local Docker stack, a hosted
Supabase project or Amazon RDS. By default it uses the local stack's database container; set
AXIS_DB_URL (a postgresql:// URL) for any other Postgres. --db-url works too, but like any argument
it shows up in `ps`, so prefer the variable when the URL holds a password.

A token is generated here, shown once (or written to a mode-600 file with --out) and only its
SHA-256 is stored. Never paste a token into a chat, a ticket or a log.

  axis_admin.py issue  EMAIL "Display Name" [--github LOGIN] [--out FILE]
  axis_admin.py revoke EMAIL
  axis_admin.py forget EMAIL      # deletes the dev's events (the opt-out deletion request)
  axis_admin.py list
"""
import argparse
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

LOCAL_DB_CONTAINER = "supabase_db_creai-axis-telemetry"


def split_password(db_url):
    """Return (URL without its password, password or None). The password goes to psql through
    PGPASSWORD, because psql's arguments are visible to every user on the machine via `ps`. libpq takes
    a password before the `@` or as `?password=`, so both are removed; the query string's wins."""
    parts = urllib.parse.urlsplit(db_url)
    password = urllib.parse.unquote(parts.password) if parts.password is not None else None
    netloc = parts.netloc
    if parts.password is not None:
        netloc = netloc.rsplit("@", 1)[1]
        if parts.username is not None:
            netloc = urllib.parse.quote(urllib.parse.unquote(parts.username), safe="") + "@" + netloc
    query = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if key == "password":
            password = value
        else:
            query.append((key, value))
    if password is None:
        return db_url, None
    query = urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc, query=query)), password


def psql(args, sql, **params):
    """Run `sql` with psql variables (`:'name'` in the SQL), returning unaligned rows."""
    db_url = args.db_url or os.environ.get("AXIS_DB_URL")
    env = None
    if db_url:
        if not shutil.which("psql"):
            sys.exit("psql is not installed (macOS: brew install libpq).")
        if "://" not in db_url:
            sys.exit("Give the database as a postgresql:// URL.")
        db_url, password = split_password(db_url)
        if password is not None:
            env = {**os.environ, "PGPASSWORD": password}
        cmd = ["psql", db_url]
    else:
        cmd = ["docker", "exec", "-i", LOCAL_DB_CONTAINER, "psql", "-U", "postgres", "-d", "postgres"]
    cmd += ["-X", "-q", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1"]
    for name, value in params.items():
        cmd += ["-v", f"{name}={value}"]
    proc = subprocess.run(cmd, input=sql, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        sys.exit(proc.stderr.strip() or f"psql exited with {proc.returncode}")
    return [line.split("\t") for line in proc.stdout.splitlines() if line]


def open_private(path):
    """Create `path` as a new file readable by the owner only. An existing path is refused rather than
    reused: another process may already hold it open, and in a shared folder it may be a planted link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(Path(path), flags, 0o600)
    except FileExistsError:
        sys.exit(f"{path} already exists. Give a new path; the token is only written to a file created for it.")
    return os.fdopen(fd, "w")


def cmd_issue(args):
    if not args.email.lower().endswith("@creai.mx"):
        sys.exit("Only @creai.mx addresses can opt in.")
    token = secrets.token_urlsafe(32)
    out = open_private(args.out) if args.out else None
    try:
        if out:  # on disk before the database rotates the token, so a failed write never loses the new one
            with out:
                out.write(token + "\n")
                out.flush()
                os.fsync(out.fileno())
        store_token(args, token)
    except BaseException:  # psql failures exit too; leave no file holding a token that was never stored
        if out:
            out.close()
            os.unlink(args.out)
        raise
    if out:
        print(f"Token for {args.email} written to {args.out} (mode 600). Hand it over privately, then delete the file.")
    else:
        print(f"Token for {args.email} (shown once; hand it over privately):\n{token}")
    return 0


def store_token(args, token):
    # Re-issuing rotates the token: the old one stops working at once, and a revoked dev is reinstated.
    psql(args, """
        insert into public.axis_usage_devs (email, display_name, github_login, token_hash)
        values (lower(:'email'), :'name', nullif(:'github', ''), :'hash')
        on conflict (email) do update
          set display_name = excluded.display_name,
              github_login = coalesce(excluded.github_login, axis_usage_devs.github_login),
              token_hash = excluded.token_hash,
              revoked_at = null;
        """, email=args.email, name=args.name, github=args.github or "",
         hash=hashlib.sha256(token.encode()).hexdigest())


def cmd_revoke(args):
    rows = psql(args, """
        update public.axis_usage_devs set revoked_at = now()
        where email = lower(:'email') and revoked_at is null
        returning email;
        """, email=args.email)
    print(f"Revoked {args.email}." if rows else f"No active dev with email {args.email}.")
    return 0 if rows else 1


def cmd_forget(args):
    rows = psql(args, """
        with gone as (
          delete from public.axis_usage_events
          where dev_id = (select id from public.axis_usage_devs where email = lower(:'email'))
          returning 1
        )
        select count(*) from gone;
        """, email=args.email)
    print(f"Deleted {rows[0][0]} events of {args.email}. Revoke the token too if they opted out.")
    return 0


def cmd_list(args):
    rows = psql(args, """
        select d.email, d.display_name, to_char(d.opted_in_at, 'YYYY-MM-DD'),
               coalesce(to_char(d.revoked_at, 'YYYY-MM-DD'), '-'),
               count(e.id), coalesce(to_char(max(e.ts), 'YYYY-MM-DD HH24:MI'), '-')
        from public.axis_usage_devs d
        left join public.axis_usage_events e on e.dev_id = d.id
        group by d.id order by d.email;
        """)
    header = ["email", "name", "opted in", "revoked", "events", "last event"]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for r in [header, *rows]:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="creai-axis usage backend admin")
    ap.add_argument("--db-url", help="postgresql:// URL (default: AXIS_DB_URL, else the local Docker stack); "
                                     "visible in `ps`, so prefer AXIS_DB_URL")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("issue", help="issue or rotate a dev's ingest token")
    p.add_argument("email")
    p.add_argument("name")
    p.add_argument("--github")
    p.add_argument("--out", help="write the token to this new file (mode 600; must not exist yet) instead of printing it")
    p.set_defaults(fn=cmd_issue)
    p = sub.add_parser("revoke", help="revoke a dev's token")
    p.add_argument("email")
    p.set_defaults(fn=cmd_revoke)
    p = sub.add_parser("forget", help="delete all of a dev's events")
    p.add_argument("email")
    p.set_defaults(fn=cmd_forget)
    sub.add_parser("list", help="list devs with event counts").set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

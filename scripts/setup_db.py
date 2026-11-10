#!/usr/bin/env python3
"""Creates or updates the Place Finder database objects and its one login role.

    uv run python scripts/setup_db.py [--env-file PATH]

Needs the database owner's connection URL in PLACES_OWNER_URL, from the process
environment or from the env file. On the first run it generates the visitor hash
salt and the places_app password, then writes the salt and a pooled connection URL
for places_app to the env file (mode 0600). Later runs reuse them, so running it
again changes nothing. No secret is ever printed.

The env file defaults to ../site_private/.env.places next to this repository, or
to PLACES_ENV_FILE when that is set. It stays outside the repository.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = REPO_ROOT / "db" / "schema.sql"
DEFAULT_ENV_FILE = REPO_ROOT.parent / "site_private" / ".env.places"
ROLE = "places_app"
URL_VARIABLE = "PLACES_DB_URL"
SALT_VARIABLE = "PLACES_HASH_SALT"
OWNER_VARIABLE = "PLACES_OWNER_URL"
TABLES = ("geocode_cache", "daily_counts", "boroughs", "neighborhoods", "meta")
# Tables places_app may read (public data). It may not write to any table.
READABLE = {"boroughs", "neighborhoods", "meta", "places"}
# Every function places_app may run. Nothing else in the schema is executable by it or by public.
FUNCTIONS = (
    "places.geocode_get(text)",
    "places.geocode_put(text, boolean, double precision, double precision, text)",
    "places.take_slot(text, text, int, int)",
)
INTERNAL_FUNCTIONS = ("places.slot_for(text, int)",)


def fail(message: str, code: int = 2):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines as a dict. Values are data; nothing here goes near a shell."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def write_env(updates: dict[str, str], path: Path) -> None:
    """Replaces or appends keys, keeps every other line, and leaves the file 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line else ""
        if key in remaining and not line.lstrip().startswith("#"):
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())

    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".env.places.")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as temp:
            temp.write("\n".join(output) + "\n")
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def pooled_host(host: str) -> str:
    """Neon's pooled endpoint is the compute host with -pooler added to its first label."""
    if not host.endswith(".neon.tech"):
        return host
    first, _, rest = host.partition(".")
    return host if first.endswith("-pooler") else f"{first}-pooler.{rest}"


def role_url(owner_url: str, password: str) -> str:
    """The pooled URL for places_app. Drops any options parameter, which the pooler rejects."""
    parts = urlsplit(owner_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "options"]
    if not any(k == "sslmode" for k, _ in query):
        query.append(("sslmode", "require"))
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{quote(ROLE, safe='')}:{quote(password, safe='')}@{pooled_host(parts.hostname)}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))


def existing_password(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    if parts.username == ROLE and parts.password:
        return unquote(parts.password)
    return None


def plan(env: dict[str, str], owner_url: str) -> tuple[dict[str, str], str]:
    """(env updates, the role password). Reuses anything already generated."""
    updates: dict[str, str] = {}
    if not env.get(SALT_VARIABLE):
        updates[SALT_VARIABLE] = secrets.token_hex(32)
    password = existing_password(env.get(URL_VARIABLE)) or secrets.token_urlsafe(32)
    url = role_url(owner_url, password)
    if env.get(URL_VARIABLE) != url:
        updates[URL_VARIABLE] = url
    return updates, password


def verify(conn) -> list[str]:
    """Checks the live grants against the spec above. Returns one line per problem."""
    problems: list[str] = []
    login = conn.execute("select rolcanlogin from pg_roles where rolname = %s", (ROLE,)).fetchone()
    if not login or not login[0]:
        problems.append(f"{ROLE}: cannot log in")
    tables = list(TABLES)
    if conn.execute("select to_regclass('places.places') is not null").fetchone()[0]:
        tables.append("places")  # created by the first data load
    for table in tables:
        for privilege in ("select", "insert", "update", "delete", "truncate", "references", "trigger"):
            for grantee in ("public", ROLE):
                has = conn.execute(
                    "select has_table_privilege(%s, %s, %s)", (grantee, f"places.{table}", privilege)
                ).fetchone()[0]
                expected = grantee == ROLE and privilege == "select" and table in READABLE
                if has != expected:
                    problems.append(f"{grantee}: unexpected {privilege} state on places.{table}")
    for function in FUNCTIONS + INTERNAL_FUNCTIONS:
        for grantee in ("public", ROLE):
            has = conn.execute(
                "select has_function_privilege(%s, %s, 'execute')", (grantee, function)
            ).fetchone()[0]
            if has != (grantee == ROLE and function in FUNCTIONS):
                problems.append(f"{grantee}: unexpected execute state on {function}")
    # Postgres spells signatures its own way ("integer", not "int"), so compare its spelling.
    canonical = {f: conn.execute("select %s::regprocedure::text", (f,)).fetchone()[0]
                 for f in FUNCTIONS + INTERNAL_FUNCTIONS}
    internal = {canonical[f] for f in INTERNAL_FUNCTIONS}
    rows = conn.execute(
        "select p.oid::regprocedure::text, p.prosecdef, coalesce(array_to_string(p.proconfig, ','), '') "
        "from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'places'"
    ).fetchall()
    for signature, definer, config in rows:
        if signature not in canonical.values():
            problems.append(f"unlisted function {signature}")
        if "search_path=" not in config:
            problems.append(f"{signature}: no fixed search_path")
        if definer == (signature in internal):
            problems.append(f"{signature}: unexpected security definer state")
    return problems


def main() -> None:
    import psycopg
    from psycopg import sql

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=Path(os.environ.get("PLACES_ENV_FILE") or DEFAULT_ENV_FILE))
    args = parser.parse_args()

    env = read_env(args.env_file)
    owner_url = os.environ.get(OWNER_VARIABLE) or env.get(OWNER_VARIABLE, "")
    if not owner_url:
        fail(f"{OWNER_VARIABLE} is not set in the environment or in {args.env_file.name}")
    parts = urlsplit(owner_url)
    if parts.scheme not in ("postgres", "postgresql") or not parts.hostname:
        fail(f"{OWNER_VARIABLE} is not a postgres connection URL")
    if not SCHEMA_FILE.exists():
        fail("db/schema.sql was not found")

    updates, password = plan(env, owner_url)
    # Written before the database work, so a failed run reuses the same values next time.
    if updates:
        write_env(updates, args.env_file)

    try:
        conn = psycopg.connect(owner_url, autocommit=True, connect_timeout=15, prepare_threshold=None)
    except psycopg.Error as exc:
        fail(f"could not connect to the database ({type(exc).__name__})")
    try:
        with conn.transaction():
            conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
            conn.execute(sql.SQL("alter role {} login password {}").format(sql.Identifier(ROLE), sql.Literal(password)))
        problems = verify(conn)
    except psycopg.Error as exc:
        fail(f"database step failed ({type(exc).__name__}, sqlstate {exc.sqlstate})")
    finally:
        conn.close()

    if problems:
        for line in problems:
            print(f"grant check: {line}", file=sys.stderr)
        raise SystemExit(1)
    print(f"schema ok, role {ROLE} ok, grants verified, {len(updates)} env values written to {args.env_file}")


if __name__ == "__main__":
    main()

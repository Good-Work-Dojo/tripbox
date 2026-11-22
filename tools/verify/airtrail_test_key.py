#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Inserts the API key of an end-to-end run into a throwaway AirTrail's database, the way AirTrail
stores one: the base64 SHA-256 of the NFKC-normalised key, with its scopes, for one user picked by
id or as the instance's owner.

Usage: airtrail_test_key.py CONTAINER
                            (--first-user | --second-user | --owner | --other-user | --user-id ID)
                            [--scopes A,B,...] [--name NAME] [--key-env VAR]
                            [--exec-prefix CMD] [--db-user U] [--db-name D]

The key is read from the variable --key-env names (default E2E_AIRTRAIL_API_KEY), never from an
argument. The SQL goes to `<exec prefix> CONTAINER psql ...` on stdin (default prefix
`docker exec -i`, split like a shell line). --first-user takes the user whose id sorts first,
--second-user the one whose id sorts second, --owner the instance's owner (the user AirTrail
marks `is_owner`, whose role grants everything), --other-user the first by id of the others.
--scopes names the key's scopes, each one of AirTrail's own (UPSTREAM_SCOPES), each once
(default: the six tripbox uses); --name is the key's
name in AirTrail (^[a-z0-9-]{1,40}$, default tripbox-e2e). Prints `test key inserted` and exits
0. A failing psql prints psql's own ERROR and FATAL lines (never an echoed statement line, a
caret, a hint, a detail or a context line, and no line holding eight or more consecutive
characters of the key or its hash), then
`airtrail_test_key.py: psql failed (exit N)`, and exits 1. A bad argument exits 2 before anything
runs, and so does a CONTAINER named like a real install's database (lower-cased, beginning
`airtrail`): the helper is for a throwaway test database only. Nothing printed holds the key, its
hash or a user's name: users are picked by id or by the owner's mark, and named by nothing."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shlex
import subprocess
import sys
import unicodedata

PROG = "airtrail_test_key.py"
KEY_NAME = "tripbox-e2e"
# what tripbox asks of AirTrail: its profile, reference ids, and its own flights and passengers
SCOPES = (
    "profile.read",
    "reference_data.read",
    "flight.read.own",
    "flight.create.own",
    "flight.update.own",
    "flight.passengers.manage.own",
)
# every scope AirTrail v3.14.0 knows (src/lib/api/v1/scopes.ts): those that are a permission of
# the same name, then those that are not
UPSTREAM_SCOPES = (
    "flight.read.own",
    "flight.read.any",
    "flight.create.own",
    "flight.create.any",
    "flight.update.own",
    "flight.update.any",
    "flight.delete.own",
    "flight.delete.any",
    "flight.export.own",
    "flight.export.any",
    "flight.passengers.manage.own",
    "flight.passengers.manage.any",
    "users.directory.read",
    "roles.manage",
    "profile.read",
    "preferences.write",
    "reference_data.read",
    "custom_fields.read",
    "stats.read",
    "tracks.read",
    "tracks.write",
    "visited_countries.read",
    "visited_countries.write",
    "shares.read",
    "shares.write",
)
NAME = re.compile(r"[a-z0-9-]{1,40}")
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
USER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
KEY = re.compile(r"[A-Za-z0-9_-]{32,128}")
DB_WORD = re.compile(r"[a-z][a-z0-9_]{0,62}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
# what upstream's own compose file and common installs call AirTrail and its database
REAL_INSTALL = "airtrail"
REAL_INSTALL_REFUSAL = (
    f"{PROG}: CONTAINER looks like a real install's database; this helper is for a throwaway "
    "test database only"
)
# psql's lines that name what failed; reading a file it may put `psql:<file>:<line>: ` first
PSQL_ERROR = re.compile(r"(psql:[^\s:]+:\d+: )?(ERROR|FATAL):")
# the shortest run of a secret's characters that a printed line may not hold
SECRET_RUN = 8


def key_hash(key: str) -> str:
    """The value AirTrail keeps in api_key.key for this key."""
    digest = hashlib.sha256(unicodedata.normalize("NFKC", key).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def build_sql(
    hashed: str,
    user_id: str | None,
    *,
    scopes: tuple[str, ...] = SCOPES,
    name: str = KEY_NAME,
    skip: int = 0,
    owner: bool | None = None,
) -> str:
    """The statements psql runs: fail unless the user exists, then insert one key for it, named
    `name`, holding `scopes`. With no `user_id` the user is the one whose id sorts after `skip`
    others, among the instance's owners (`owner` True), among the users who are not its owner
    (False) or among all (None). Every value is checked against a closed alphabet or list before
    it reaches this text."""
    if user_id is not None:
        where = f" WHERE id = '{user_id}'"
    elif owner is not None:
        where = " WHERE is_owner" if owner else " WHERE NOT is_owner"
    else:
        where = ""
    offset = f" OFFSET {skip}" if user_id is None and skip else ""
    listed = ",".join(f"'{s}'" for s in scopes)
    return (
        "DO $$ BEGIN\n"
        f'  IF NOT EXISTS (SELECT 1 FROM "user"{where} ORDER BY id{offset} LIMIT 1) THEN\n'
        "    RAISE EXCEPTION 'no such AirTrail user';\n"
        "  END IF;\n"
        "END $$;\n"
        "INSERT INTO api_key (name, user_id, key, scopes)\n"
        f"SELECT '{name}', id, '{hashed}',\n"
        f"       ARRAY[{listed}]\n"
        f'FROM "user"{where} ORDER BY id{offset} LIMIT 1;\n'
    )


def parse(argv: list[str] | None) -> tuple[argparse.Namespace, list[str], str]:
    """The arguments, the exec prefix and the key; exits 2 on anything malformed."""
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    parser.add_argument("container", help="the database container or compose service")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--first-user", action="store_true", help="the user whose id sorts first")
    who.add_argument(
        "--second-user", action="store_true", help="the user whose id sorts second"
    )
    who.add_argument(
        "--owner", action="store_true", help="the instance's owner (by the owner flag)"
    )
    who.add_argument(
        "--other-user",
        action="store_true",
        help="the user whose id sorts first among those who are not the instance's owner",
    )
    who.add_argument("--user-id", help="the user with this id")
    parser.add_argument("--scopes", help="the key's scopes, comma-separated (default: the six)")
    parser.add_argument("--name", default=KEY_NAME, help="the key's name in AirTrail")
    parser.add_argument("--key-env", default="E2E_AIRTRAIL_API_KEY")
    parser.add_argument("--exec-prefix", default="docker exec -i")
    parser.add_argument("--db-user", default="flightlog")
    parser.add_argument("--db-name", default="flightlog")
    args = parser.parse_args(argv)
    if not CONTAINER.fullmatch(args.container):
        parser.error("CONTAINER must be a container or service name")
    if args.container.lower().startswith(REAL_INSTALL):
        print(REAL_INSTALL_REFUSAL, file=sys.stderr)
        sys.exit(2)
    if args.user_id is not None and not USER_ID.fullmatch(args.user_id):
        parser.error(f"--user-id must match ^{USER_ID.pattern}$")
    for flag, value in (("--db-user", args.db_user), ("--db-name", args.db_name)):
        if not DB_WORD.fullmatch(value):
            parser.error(f"{flag} must match ^{DB_WORD.pattern}$")
    if not NAME.fullmatch(args.name):
        parser.error(f"--name must match ^{NAME.pattern}$")
    if args.scopes is None:
        args.scopes = SCOPES
    else:
        named = tuple(args.scopes.split(","))
        unknown = [s for s in named if s not in UPSTREAM_SCOPES]
        if unknown or not named or len(set(named)) != len(named):
            parser.error("--scopes must name AirTrail's own scopes, each once, joined by commas")
        args.scopes = named
    if not ENV_NAME.fullmatch(args.key_env):
        parser.error("--key-env must name an environment variable")
    try:
        prefix = shlex.split(args.exec_prefix)
    except ValueError:
        parser.error("--exec-prefix is not a command line")
    if not prefix:
        parser.error("--exec-prefix is empty")
    key = os.environ.get(args.key_env, "")
    if not KEY.fullmatch(key):
        parser.error(
            f"the key in {args.key_env} must be 32 to 128 characters of A-Z, a-z, 0-9, _ and -"
        )
    return args, prefix, key


def holds_secret(line: str, secrets: tuple[str, ...]) -> bool:
    """True when the line holds SECRET_RUN or more consecutive characters of any secret."""
    return any(
        s[i : i + SECRET_RUN] in line for s in secrets for i in range(len(s) - SECRET_RUN + 1)
    )


def shown_errors(stderr: str, secrets: tuple[str, ...]) -> list[str]:
    """psql's ERROR and FATAL lines, which name the failing object. The rest is never shown: psql
    echoes the failing statement line cut to a window around the error, so a piece of the hash can
    stand there alone, and a line holding a piece of a secret is dropped whatever it is."""
    return [
        line
        for line in stderr.splitlines()
        if PSQL_ERROR.match(line) and not holds_secret(line, secrets)
    ]


def main(argv: list[str] | None = None) -> int:
    args, prefix, key = parse(argv)
    hashed = key_hash(key)
    cmd = [*prefix, args.container, "psql", "-v", "ON_ERROR_STOP=1", "-q"]
    cmd += ["-U", args.db_user, "-d", args.db_name]
    sql = build_sql(
        hashed,
        args.user_id,
        scopes=args.scopes,
        name=args.name,
        skip=1 if args.second_user else 0,
        owner=True if args.owner else False if args.other_user else None,
    )
    result = subprocess.run(cmd, input=sql, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        for line in shown_errors(result.stderr or "", (hashed, key)):
            print(line, file=sys.stderr)
        print(f"{PROG}: psql failed (exit {result.returncode})", file=sys.stderr)
        return 1
    print("test key inserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())

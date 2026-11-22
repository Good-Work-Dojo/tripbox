# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""tools/verify/airtrail_test_key.py: the API key of the end-to-end run, inserted into a throwaway
AirTrail's database as AirTrail stores one (the hash, never the key), for one user picked by id.

psql is never run here: `subprocess.run` is patched, and each test reads the command and the SQL it
was given. The key is invented here and must appear in no command, SQL or output."""

import base64
import hashlib
import importlib.util
import shlex
import sys
import unicodedata
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "airtrail_test_key.py"
_spec = importlib.util.spec_from_file_location("airtrail_test_key", SCRIPT)
tk = importlib.util.module_from_spec(_spec)
sys.modules["airtrail_test_key"] = tk
_spec.loader.exec_module(tk)

KEY = "Qx9-Vz8_Wy7Rt6Su5Ts4Ur3Vq2Wp1Xo0Yn"  # 34 characters of the token alphabet
SCOPES = [
    "profile.read",
    "reference_data.read",
    "flight.read.own",
    "flight.create.own",
    "flight.update.own",
    "flight.passengers.manage.own",
]
PSQL = ["psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", "flightlog", "-d", "flightlog"]


class Recorder:
    """Stands in for subprocess.run: records each call and answers with `returncode` and
    `stderr`."""

    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return tk.subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)


@pytest.fixture
def psql(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(tk.subprocess, "run", recorder)
    monkeypatch.setenv("E2E_AIRTRAIL_API_KEY", KEY)
    return recorder


def run(capsys, *argv):
    """The script's exit status and its stdout and stderr."""
    try:
        status = tk.main(list(argv))
    except SystemExit as exc:
        status = exc.code
    out, err = capsys.readouterr()
    return status, out, err


def expected_hash(key):
    digest = hashlib.sha256(unicodedata.normalize("NFKC", key).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def test_the_container_argument_is_required(psql, capsys):
    status, out, err = run(capsys, "--first-user")
    assert status == 2 and psql.calls == [] and out == ""
    assert KEY not in err


def test_the_sql_holds_the_hash_and_never_the_key(psql, capsys):
    status, out, err = run(capsys, "flightlogdb", "--user-id", "u_9-Sample")
    assert status == 0
    ((cmd, kwargs),) = psql.calls
    sql = kwargs["input"]
    assert f"'{expected_hash(KEY)}'" in sql
    assert "ARRAY[" + ",".join(f"'{s}'" for s in SCOPES) + "]" in "".join(sql.split())
    assert sql.count("WHERE id = 'u_9-Sample'") == 2
    assert KEY not in sql and KEY not in " ".join(cmd) and KEY not in out + err
    assert expected_hash(KEY) not in out + err



def test_a_failed_psql_shows_its_error_lines_and_no_piece_of_a_secret(psql, capsys):
    """psql echoes the failing line of the statement (numbered within that statement, cut to a
    window around the error, so a piece of the hash may stand alone) with a caret line, and may add
    a hint or context: only its ERROR and FATAL lines pass, and none holding a piece of a secret."""
    hashed = expected_hash(KEY)
    psql.returncode = 3
    psql.stderr = "\n".join([
        'ERROR:  column "id" does not exist',
        f"LINE 2: SELECT 'tripbox-e2e', id, '{hashed[:27]}...",
        "                              ^",
        'HINT:  Perhaps you meant to reference the column "user.uid".',
        f'ERROR:  invalid input syntax for type text: "{hashed[9:21]}"',
        f'FATAL:  token "{KEY[5:13]}" refused',
        "DETAIL:  Key (user_id)=(u9) is not present in table \"user\".",
        "CONTEXT:  PL/pgSQL function inline_code_block line 3 at RAISE",
        "FATAL:  terminating connection due to administrator command",
    ])
    status, out, err = run(capsys, "flightlogdb", "--first-user")
    assert status == 1 and out == ""
    assert err.splitlines() == [
        'ERROR:  column "id" does not exist',
        "FATAL:  terminating connection due to administrator command",
        "airtrail_test_key.py: psql failed (exit 3)",
    ]


def test_a_bad_user_id_or_key_is_refused_before_anything_runs(psql, capsys, monkeypatch):
    for argv in (
        ["flightlogdb", "--user-id", "u9'; DROP TABLE api_key; --"],
        ["flightlogdb", "--user-id", ""],
        ["flightlogdb", "--user-id", "u" * 65],
        ["flightlogdb"],
        ["flightlogdb", "--first-user", "--user-id", "u9"],
        ["flightlogdb", "--first-user", "--db-user", "Flightlog"],
        ["flightlogdb", "--first-user", "--db-name", "flightlog;"],
        ["flightlogdb", "--first-user", "--key-env", "NOT-A-NAME"],
        ["flightlogdb", "--first-user", "--exec-prefix", ""],
        ["flight logdb", "--first-user"],
    ):
        status, out, err = run(capsys, *argv)
        assert (status, psql.calls, out) == (2, [], ""), argv
        assert KEY not in err
    for key in ("Qx9Vz8Wy7R", "Qx9 Vz8 Wy7 Rt6 Su5 Ts4 Ur3 Vq2 Wp1", "Q" * 129, "", None):
        if key is None:
            monkeypatch.delenv("E2E_AIRTRAIL_API_KEY")
        else:
            monkeypatch.setenv("E2E_AIRTRAIL_API_KEY", key)
        status, out, err = run(capsys, "flightlogdb", "--first-user")
        assert (status, psql.calls, out) == (2, [], ""), key
        assert not key or key not in err


def test_a_container_named_like_a_real_install_is_refused_before_anything_runs(psql, capsys):
    refusal = (
        "airtrail_test_key.py: CONTAINER looks like a real install's database; this helper is for "
        "a throwaway test database only"
    )
    for name in ("airtrail_db", "AirTrail", "airtrail-db-1"):
        status, out, err = run(capsys, name, "--first-user")
        assert (status, psql.calls, out) == (2, [], ""), name
        assert err.splitlines() == [refusal]
    for name in ("flightlogdb", "tripbox-x-flightlogdb-1"):
        status, out, _err = run(capsys, name, "--first-user")
        assert (status, out) == (0, "test key inserted\n"), name
    assert [cmd[3] for cmd, _kwargs in psql.calls] == ["flightlogdb", "tripbox-x-flightlogdb-1"]


def test_first_user_selects_by_id_order_and_names_nobody(psql, capsys):
    status, out, err = run(capsys, "flightlogdb", "--first-user")
    assert status == 0 and out == "test key inserted\n"
    ((_cmd, kwargs),) = psql.calls
    sql = kwargs["input"]
    assert "ORDER BY id LIMIT 1" in sql and "WHERE" not in sql
    assert "username" not in sql
    assert "RAISE EXCEPTION 'no such AirTrail user'" in sql


def test_the_command_is_the_prefix_the_container_and_psql(psql, capsys, monkeypatch):
    monkeypatch.setenv("OTHER_KEY_VAR", KEY[::-1])
    prefix = "'/repo with space/bin/compose.sh' e2e-test exec -T"
    status, _out, _err = run(
        capsys, "flightlogdb", "--first-user", "--exec-prefix", prefix, "--key-env", "OTHER_KEY_VAR"
    )
    assert status == 0
    ((cmd, kwargs),) = psql.calls
    assert cmd == [*shlex.split(prefix), "flightlogdb", *PSQL]
    assert f"'{expected_hash(KEY[::-1])}'" in kwargs["input"]
    assert kwargs.get("shell") is not True
    # the default prefix; another user and database
    psql.calls.clear()
    status, _out, _err = run(
        capsys, "flightlogdb", "--first-user", "--db-user", "e2e", "--db-name", "log"
    )
    assert status == 0
    ((cmd, _kwargs),) = psql.calls
    assert cmd == ["docker", "exec", "-i", "flightlogdb", *PSQL[:5], "e2e", "-d", "log"]


def test_scopes_are_a_closed_list_and_the_default_is_the_six(psql, capsys):
    assert tk.SCOPES == tuple(SCOPES) and set(tk.SCOPES) <= set(tk.UPSTREAM_SCOPES)
    assert len(tk.UPSTREAM_SCOPES) == len(set(tk.UPSTREAM_SCOPES)) == 25
    hand = [*SCOPES, "flight.delete.own"]
    status, out, _err = run(
        capsys, "flightlogdb", "--first-user", "--scopes", ",".join(hand), "--name", "hand-2"
    )
    assert (status, out) == (0, "test key inserted\n")
    ((_cmd, kwargs),) = psql.calls
    sql = "".join(kwargs["input"].split())
    assert "ARRAY[" + ",".join(f"'{s}'" for s in hand) + "]" in sql
    assert "SELECT'hand-2',id," in sql
    psql.calls.clear()
    for argv in (
        ["--scopes", "flight.read.own,flight.fly.own"],
        ["--scopes", "flight.read.own,flight.read.own"],
        ["--scopes", ""],
        ["--scopes", "flight.read.own,"],
        ["--scopes", "flight.read.own; DROP TABLE api_key"],
        ["--scopes", "weather.read"],  # an older AirTrail's scope, not one v3.14.0 knows
        ["--name", "Tripbox"],
        ["--name", "x" * 41],
        ["--name", "a'b"],
    ):
        status, out, err = run(capsys, "flightlogdb", "--first-user", *argv)
        assert (status, psql.calls, out) == (2, [], ""), argv
        assert KEY not in err


def test_the_second_user_is_picked_by_id(psql, capsys):
    status, out, err = run(capsys, "flightlogdb", "--second-user")
    assert (status, out) == (0, "test key inserted\n")
    ((_cmd, kwargs),) = psql.calls
    sql = kwargs["input"]
    # the existence check and the insert pick the same row: the second by id, nobody by name
    assert sql.count('FROM "user" ORDER BY id OFFSET 1 LIMIT 1') == 2
    assert "WHERE" not in sql and "username" not in sql
    assert KEY not in sql and KEY not in out + err
    for argv in (
        ["--second-user", "--first-user"],
        ["--second-user", "--user-id", "u9"],
        ["--owner", "--other-user"],
        ["--owner", "--first-user"],
    ):
        status, out, _err = run(capsys, "flightlogdb", *argv)
        assert (status, out) == (2, ""), argv
    assert len(psql.calls) == 1


def test_the_owner_is_picked_by_its_mark_and_the_other_user_is_never_the_owner(psql, capsys):
    for flag, where in (("--owner", "WHERE is_owner"), ("--other-user", "WHERE NOT is_owner")):
        status, out, err = run(capsys, "flightlogdb", flag)
        assert (status, out) == (0, "test key inserted\n"), flag
        ((_cmd, kwargs),) = psql.calls
        sql = kwargs["input"]
        # the existence check and the insert pick the same row, by the mark and then by id
        assert sql.count(f'FROM "user" {where} ORDER BY id LIMIT 1') == 2, flag
        assert "OFFSET" not in sql and "username" not in sql
        assert KEY not in sql and KEY not in out + err
        psql.calls.clear()

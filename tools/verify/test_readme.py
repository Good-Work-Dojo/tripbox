# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The README's blocks are what a stranger types and pastes, so each is held to the code and to
the compose file: the quick start is three commands and the compose file's head says the same,
the sample is read with no model into one trip, each mailbox block is a whole valid set of
settings, and the pictures are the four, small and free of text chunks."""

import json
import re
import struct
from pathlib import Path

import pytest
from readme_blocks import BlockError, blocks

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
NAMES = {
    "quickstart", "server", "sample", "doctor", "mailbox-gmail", "mailbox-other", "backup",
    "upgrade", "report",
}  # fmt: skip
RELEASE = "https://github.com/Good-Work-Dojo/tripbox/releases/latest/download/tripbox.tar.gz"
PICTURES = {"trips-390.png", "trip-390.png", "trips-1280.png", "trip-1280.png"}
TEXT_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"eXIf"}
MAX_PICTURE = 1_048_576


def section(heading: str) -> str:
    """The README from a heading to the next heading of its level or above."""
    found = re.search(rf"^(#+) {re.escape(heading)}\s*$", README, re.M)
    assert found, heading
    rest = README[found.end() :]
    end = re.search(rf"^#{{1,{len(found.group(1))}}} ", rest, re.M)
    return rest[: end.start()] if end else rest


def lines_of(name: str) -> list[str]:
    return [ln for ln in blocks(README)[name].splitlines() if ln.strip()]


# ----------------------------------------------------------------------------- the parser
def test_the_blocks_parser_names_what_is_wrong():
    assert blocks("<!-- block: a -->\n\n```sh\nx\ny\n```\n") == {"a": "x\ny"}
    with pytest.raises(BlockError, match="twice"):
        blocks("<!-- block: a -->\n```\nx\n```\n<!-- block: a -->\n```\ny\n```\n")
    with pytest.raises(BlockError, match="not followed"):
        blocks("<!-- block: a -->\ntext\n```\nx\n```\n")
    with pytest.raises(BlockError, match="not followed"):
        blocks("<!-- block: a -->\n")
    with pytest.raises(BlockError, match="never closed"):
        blocks("<!-- block: a -->\n```\nx\n")


def test_the_readme_holds_its_nine_blocks():
    assert set(blocks(README)) == NAMES


def test_the_quick_start_is_three_commands():
    assert lines_of("quickstart") == [
        f"curl -fsSL {RELEASE} | tar -xz && cd tripbox",
        "(umask 077 && echo 'TRIPBOX_PASSWORD=<choose a password of 10 characters or more>'"
        " > .env)",
        "docker compose up -d",
    ]


def compose_head() -> list[str]:
    """The quick start's commands as the compose file's opening comment gives them: the lines
    that are indented under 'Quick start', a trailing backslash joining a line to the next."""
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    comment = [ln[1:].rstrip() for ln in text.splitlines() if ln.startswith("#")]
    start = next(n for n, ln in enumerate(comment) if ln.lstrip().startswith("Quick start"))
    out, held = [], ""
    for ln in comment[start + 1 :]:
        if not ln.startswith("   ") or ln.strip().startswith("then "):
            break
        piece = ln.strip()
        if piece.endswith("\\"):
            held += piece[:-1].strip() + " "
            continue
        out.append(re.sub(r"\s+", " ", held + piece))
        held = ""
    return out


def test_the_quick_start_in_the_compose_file_is_the_readmes():
    assert compose_head() == [re.sub(r"\s+", " ", ln) for ln in lines_of("quickstart")]


def test_the_quick_start_says_how_a_dollar_is_written():
    after = section("Quick start").split("On a server")[0]
    assert "`$`" in after and "`$$`" in after and ".env" in after
    # the sentence follows the commands, before the server's two lines
    assert after.index("```", after.index("docker compose up -d")) < after.index("`$$`")


def test_the_server_block_is_the_two_lines():
    assert lines_of("server") == [
        "TRIPBOX_LISTEN=0.0.0.0:8110",
        "TRIPBOX_BASE_URL=http://<its name or address>:8110",
    ]
    assert "home network only" in section("On a server")
    # a taken port, the folder `.env` goes in, and what to run after editing it
    quick = section("Quick start").split("On a server")[0]
    assert "TRIPBOX_LISTEN=127.0.0.1:8111" in quick
    assert "TRIPBOX_BASE_URL=http://localhost:8111" in quick
    assert "inside the new `tripbox` folder" in quick and "sign-in page" in quick
    assert "docker compose up -d` again" in quick
    assert "brackets included" in quick and "fewer than 10 characters" in quick
    assert ">> .env" in quick and "`>>`" in quick and "http://localhost:8111" in quick
    assert "mail password included" in quick


def test_the_doctor_block_is_the_command_the_docs_give():
    assert lines_of("doctor") == ["docker compose run --rm worker tripbox doctor"]


# ----------------------------------------------------------------------------- the sample
def file_it(raw: bytes, tmp_path: Path):
    """The pasted bytes as the worker reads them, no model: an email is read from its first
    header on and filed by the extractor in this image, under the corpus's own settings."""
    from tripbox import corpus, uploads
    from tripbox.ledger import Ledger
    from tripbox.pipeline.run import build_context, process_raw

    assert uploads.sniff(raw) == "eml"
    c = corpus.load(corpus.default_dir())
    s = corpus.settings(
        c, data_dir=tmp_path / "d", archive_dir=tmp_path / "a", logs_dir=tmp_path / "l"
    )
    ledger = Ledger.memory()
    with corpus.process_as(s):
        ctx = build_context(s, ledger=ledger, with_model=False, with_notifier=False)
        process_raw(ctx, uploads.email_of(raw) or raw, source="paste", trusted=True, notify=False)
    return ledger


def one_trip_one_flight(ledger) -> None:
    trips = ledger.conn.execute("SELECT id FROM trips").fetchall()
    items = ledger.conn.execute("SELECT kind, payload_json FROM trip_items").fetchall()
    assert len(trips) == 1
    assert [row["kind"] for row in items] == ["flight"]
    assert "Alex Sample" in json.loads(items[0]["payload_json"])["passengers"]


def test_the_readme_sample_becomes_one_trip_with_no_model(tmp_path):
    sample = blocks(README)["sample"]
    assert len(sample.splitlines()) <= 40
    assert "as one text" in section("Try it")
    for n, raw in enumerate(
        (
            sample.encode(),
            sample.replace("\n", "\r\n").encode(),
            (sample + "\n\n\n").encode(),
            ("\n\n" + sample).encode(),
        )
    ):
        one_trip_one_flight(file_it(raw, tmp_path / str(n)))


# ----------------------------------------------------------------------------- the mailbox
STAND_IN = {"an app password": "abcdefghijklmnop"}


def settings_of(name: str, tmp_path: Path):
    from tripbox.config import Settings

    env = {"TRIPBOX_CONFIG_DIR": str(tmp_path), "TRIPBOX_PASSWORD": "a sample password"}
    for ln in lines_of(name):
        key, _, value = ln.partition("=")
        env[key] = re.sub(r"<([^>]*)>", lambda m: STAND_IN.get(m.group(1), "stand-in"), value)
    return Settings.from_env(env), env


@pytest.mark.parametrize("name", ["mailbox-gmail", "mailbox-other"])
def test_each_mailbox_block_validates_with_no_problem(name, tmp_path):
    s, env = settings_of(name, tmp_path)
    assert {f.name: f.state for f in s.features}["mail"] == "on"
    assert s.problems == ()
    # Google shows an app password in groups with spaces: with them or without, one password
    from tripbox.config import Settings

    spaced = Settings.from_env({**env, "TRIPBOX_MAIL_PASSWORD": "abcd efgh ijkl mnop"})
    assert spaced.mail_password == "abcdefghijklmnop"
    assert {"TRIPBOX_MAIL_HOST", "TRIPBOX_MAIL_USER", "TRIPBOX_MAIL_PASSWORD"} <= set(env)
    assert env["TRIPBOX_MAIL_USER"] == "trips@example.com"
    assert env["TRIPBOX_MAIL_ALLOWED_SENDERS"] == "alex@example.com"
    assert (env.get("TRIPBOX_MAIL_TRUST") == "mailbox") == (name == "mailbox-other")


def test_the_mailbox_section_names_the_status_card_and_doctor():
    text = section("Connect a mailbox")
    assert "status card" in text and "tripbox doctor" in text
    assert "without the spaces" in text and "tripbox poll" in text and "tripbox check" in text
    assert "docker compose stop worker" in text and "without stopping" in text
    assert "from then on" in text


# ----------------------------------------------------------------------------- the pictures
def png_chunks(data: bytes) -> list[bytes]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    out, at = [], 8
    while at < len(data):
        (size,) = struct.unpack(">I", data[at : at + 4])
        out.append(data[at + 4 : at + 8])
        at += 12 + size
    assert at == len(data)
    return out


def test_every_picture_the_readme_shows_is_one_of_the_four_and_small():
    shown = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", README)
    assert {Path(p).name for p in shown} == PICTURES
    assert all(p == f"docs/img/{Path(p).name}" for p in shown)
    for name in PICTURES:
        data = (ROOT / "docs" / "img" / name).read_bytes()
        assert len(data) <= MAX_PICTURE, name
        assert not TEXT_CHUNKS & set(png_chunks(data)), name


# ----------------------------------------------------------------------------- upgrade
def test_the_upgrade_block_backs_up_before_it_pulls():
    lines = lines_of("upgrade")
    backup = next(n for n, ln in enumerate(lines) if "tripbox backup" in ln)
    pull = next(n for n, ln in enumerate(lines) if "docker compose pull" in ln)
    assert backup < pull
    assert any(ln == "docker compose up -d" for ln in lines[pull:])


def test_leaving_says_what_down_removes_and_what_stays():
    text = section("Leaving")
    assert "docker compose down" in text and "down -v` removes nothing more" in text
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert not re.search(r"^volumes:", compose, re.M)  # no named volume for -v to remove
    assert "with the stack running or stopped" in section("Check your setup")

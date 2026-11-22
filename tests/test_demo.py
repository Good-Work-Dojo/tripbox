# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""`tripbox demo seed`: part of the corpus filed through the real pipeline, with the corpus's own
answers, into a new ledger whose days all count from a given today. The clock is held while each
email is filed and every date an email gives moves by the same days, so two seeds with the same
today are identical and the story is the same whatever today is."""

import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from html import escape

import pytest
from helpers.web import ORIGIN
from starlette.testclient import TestClient

from tripbox import cli, corpus, demo, util
from tripbox.config import Settings
from tripbox.ledger import Ledger
from tripbox.mail.normalize import NormalizedMessage, normalize
from tripbox.parse.models import Extraction, Reservation, Segment
from tripbox.pipeline import run
from tripbox.pipeline.run import build_context, process_raw
from tripbox.pipeline.shift import DateShift
from tripbox.trips.store import TripStore
from tripbox.validate import checks
from tripbox.validate.passengers import PassengerBook
from tripbox.web.app import create_app


def seeded_line(today: str) -> str:
    """The first line `demo seed` prints for the shipped corpus."""
    return f"demo: 21 emails filed into 7 trips (22 bookings, 1 unsorted), today {today}"


ANCHOR_LINE = seeded_line("2027-06-16")
# today on the anchor; on a day that puts a Seattle flight on a day US clocks change; on one that
# puts the Munich-London flight on a day European clocks change; across the year's end; a leap day
TODAYS = (
    "2027-06-16",
    "2027-03-16",
    "2027-11-09",
    "2027-03-03",
    "2027-10-06",
    "2027-12-31",
    "2028-02-29",
)


def dirs(base) -> dict:
    return {
        "data_dir": base / "data",
        "archive_dir": base / "archive",
        "logs_dir": base / "logs",
    }


def first_difference(want: str, got: str) -> str:
    """Where two texts part, by line."""
    for n, (w, g) in enumerate(zip(want.splitlines(), got.splitlines(), strict=False), 1):
        if w != g:
            return f"line {n}: want {w!r}, got {g!r}"
    return f"{len(want.splitlines())} lines against {len(got.splitlines())}"


def view_text(view: dict) -> str:
    return json.dumps(view, indent=1, sort_keys=True) + "\n"


def usual_names(book: PassengerBook, names: list[str]) -> list[str]:
    return sorted({book.match(n).canonical for n in names})


class SeenDates:
    """The corpus's answers standing in for the model, noting the date of each message it is
    handed."""

    def __init__(self, answers: corpus.CorpusAnswers) -> None:
        self.answers = answers
        self.model = answers.model
        self.dates: list = []

    def extract(self, nm, hint=None):
        self.dates.append(nm.email_date)
        return self.answers.extract(nm, hint)


def booking_with_every_date() -> Extraction:
    return Extraction(
        document_type="booking",
        reservations=[
            Reservation(
                kind="flight",
                confirmation_code="QWS987",
                passengers=["Alex Sample"],
                segments=[
                    Segment(
                        carrier_iata="DL",
                        flight_number="9412",
                        origin="MSP",
                        destination="BOS",
                        departure_local="2027-03-11T08:05",
                        arrival_local="2027-03-11T11:52",
                        departure_offset="-06:00",
                        arrival_offset="-05:00",
                    ),
                    Segment(
                        carrier_iata="DL",
                        flight_number="9435",
                        origin="BOS",
                        destination="MSP",
                        departure_local="2027-03-14T17:20",
                        arrival_local="2027-03-14T19:38",
                        departure_offset="-04:00",
                        arrival_offset="-05:00",
                    ),
                ],
            ),
            Reservation(
                kind="lodging",
                confirmation_code="ZWT876",
                property_name="Example Suites Boston Seaport",
                check_in="2027-03-11T15:00",
                check_out="2027-03-14",
            ),
            Reservation(
                kind="car",
                confirmation_code="TUY654",
                provider="Example Rent A Car",
                pickup_location="DEN",
                pickup_time="2027-04-08T09:00",
                dropoff_time="2027-04-11T14:30",
            ),
        ],
    )


def a_message() -> NormalizedMessage:
    return NormalizedMessage(
        message_id="<shift@example.com>",
        subject="Your trip",
        from_addr="alex@example.com",
        date=dt.datetime(2027, 1, 25, 9, 12, tzinfo=dt.timezone(dt.timedelta(hours=-6))),
        outer_date=dt.datetime(2027, 1, 25, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5))),
        text="",
    )


def test_the_date_shift_moves_every_date_and_keeps_its_form(tmp_path):
    year = DateShift(366)
    assert year.text("2027-03-11") == "2028-03-11"
    assert year.text("2027-03-11T08:05") == "2028-03-11T08:05"
    assert year.text("not a date") == "not a date"
    assert year.text(None) is None

    ex = booking_with_every_date()
    year.extraction(ex)
    flight, stay, car = ex.reservations
    legs = [
        (s.departure_local, s.arrival_local, s.departure_offset, s.arrival_offset)
        for s in flight.segments
    ]
    assert legs == [
        ("2028-03-11T08:05", "2028-03-11T11:52", None, None),
        ("2028-03-14T17:20", "2028-03-14T19:38", None, None),
    ]
    assert (stay.check_in, stay.check_out) == ("2028-03-11T15:00", "2028-03-14")
    assert (car.pickup_time, car.dropoff_time) == ("2028-04-08T09:00", "2028-04-11T14:30")

    ex = booking_with_every_date()
    copy = ex.model_copy(deep=True)
    DateShift(0).extraction(ex)
    assert ex == copy
    assert ex.reservations[0].segments[0].departure_offset == "-06:00"

    nm = a_message()
    before = (nm.date, nm.outer_date)
    DateShift(30).message(nm)
    assert (nm.date, nm.outer_date) == tuple(d + dt.timedelta(days=30) for d in before)
    assert (nm.date.utcoffset(), nm.outer_date.utcoffset()) == tuple(
        d.utcoffset() for d in before
    )

    nm, ex = a_message(), booking_with_every_date()
    DateShift(30).apply(nm, ex)
    assert nm.date == a_message().date + dt.timedelta(days=30)
    assert ex.reservations[1].check_out == "2027-04-13"

    # through the pipeline: the extractor reads the email as written, the ledger files it moved
    c = corpus.load(corpus.default_dir())
    s = corpus.settings(c, **dirs(tmp_path))
    hotel = c.entry("05-chain-hotel")
    truth = hotel.truth["reservations"][0]
    with corpus.process_as(s):
        ctx = build_context(s, ledger=Ledger.memory(), with_model=False, with_notifier=False)
        seen = SeenDates(corpus.CorpusAnswers(c, s.llm_model))
        ctx.model = seen
        ctx.shift = DateShift(30)
        res = process_raw(ctx, hotel.raw, source="eval", notify=False)
        own = normalize(
            hotel.raw,
            s.allowed_senders,
            s.tz,
            require_auth=s.require_google_auth,
            trusted_domains=s.mail_trusted_sender_domains,
        ).email_date
        assert res.status == "processed"
        stay = ctx.trips.item_by_key("lodging|ZWT876")
        assert stay is not None
        later = [
            (dt.date.fromisoformat(truth[f][:10]) + dt.timedelta(days=30)).isoformat()
            for f in ("check_in", "check_out")
        ]
        assert [stay.start_at[:10], (stay.end_at or "")[:10]] == later
        row = ctx.ledger.messages.get(res.message_row_id)
        assert own is not None and seen.dates == [own]
        assert row["email_date"] == (own + dt.timedelta(days=30)).isoformat()
        ctx.ledger.close()


def test_the_clock_can_be_held_at_an_instant():
    t = dt.datetime(2027, 6, 16, 17, 0, tzinfo=dt.UTC)
    inner = dt.datetime(2031, 1, 2, 3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=-6)))
    with util.clock_at(t):
        assert util.now() == t and util.now().utcoffset() == dt.timedelta(0)
        assert util.now_iso() == "2027-06-16T17:00:00+00:00"
        assert run.now() == t  # imported by name, it follows too
        with util.clock_at(inner):
            assert util.now() == inner
            assert util.now_iso() == "2031-01-02T09:04:05+00:00"
        assert util.now() == t
    assert abs(util.now() - dt.datetime.now(dt.UTC)) < dt.timedelta(minutes=1)
    with pytest.raises(RuntimeError), util.clock_at(t):
        raise RuntimeError("raised inside")
    assert abs(util.now() - dt.datetime.now(dt.UTC)) < dt.timedelta(minutes=1)
    with pytest.raises(ValueError):
        with util.clock_at(dt.datetime(2027, 6, 16, 17, 0)):
            pass

    led = Ledger.memory()
    with util.clock_at(t):
        row_id = led.messages.insert(sha256="0" * 64, source="test", status="archived")
    assert led.messages.get(row_id)["created_at"] == "2027-06-16T17:00:00+00:00"
    led.close()


def test_two_seeds_with_the_same_today_are_identical(tmp_path):
    c = corpus.load(corpus.default_dir())
    assert c.anchor == dt.date(2027, 6, 16)
    # a today that is not the anchor, so every date is moved and every printed offset dropped
    today = dt.date.fromisoformat("2027-11-09")
    assert today != c.anchor
    dumps = []
    for n, held in enumerate(
        (dt.datetime(2030, 1, 1, tzinfo=dt.UTC), dt.datetime(2031, 6, 6, tzinfo=dt.UTC))
    ):
        out = tmp_path / f"here-{n}"
        with util.clock_at(held):
            seeded = demo.seed(c, out, today)
        assert seeded.failed == ()
        dumps.append(demo.dump(seeded.ledger, out / "archive"))
    # the second child also starts in a zone fourteen hours ahead of UTC
    for hash_seed, zone in (("1", {}), ("2", {"TZ": "Pacific/Kiritimati"})):
        out = tmp_path / f"child-{hash_seed}"
        done = subprocess.run(
            [sys.executable, "-m", "tripbox", "demo", "seed", "--out", str(out)]
            + ["--today", today.isoformat()],
            env={**os.environ, "PYTHONHASHSEED": hash_seed, **zone},
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.splitlines()[0] == seeded_line(today.isoformat())
        dumps.append(demo.dump(out / "data" / "ledger.db", out / "archive"))
    assert dumps[0].startswith("# ") and "\n# trip_items\n" in dumps[0]
    for n, other in enumerate(dumps[1:], 1):
        assert other == dumps[0], f"seed {n}: {first_difference(dumps[0], other)}"
    assert [line for line in dumps[0].splitlines() if str(tmp_path) in line] == []


def test_the_demo_is_the_same_story_whatever_today_is(tmp_path):
    c = corpus.load(corpus.default_dir())
    want = (c.root / "demo-view.json").read_text(encoding="utf-8")
    book = PassengerBook.from_yaml(c.root / corpus.TRAVELLERS)
    # what SCENARIOS.md promises of every demo flight's local times, held against the view
    sheet = (c.root / "SCENARIOS.md").read_text(encoding="utf-8")
    hours = re.search(r"leaves and lands between (\d\d:\d\d) and (\d\d:\d\d) local time", sheet)
    assert hours, "SCENARIOS.md states no hours for the demo's flights"
    early, late = hours.groups()
    flights = [i for t in json.loads(want)["trips"] for i in t["items"] if i["kind"] == "flight"]
    outside = [
        (i["what"], i["at"], i["until"])
        for i in flights
        if not all(isinstance(i[f], str) and early <= i[f] <= late for f in ("at", "until"))
    ]
    assert flights and outside == [], f"flights outside {early}-{late}: {outside}"
    wrong = []
    for day in TODAYS:
        today = dt.date.fromisoformat(day)
        seeded = demo.seed(c, tmp_path / day, today)
        if seeded.failed != ():
            wrong.append(f"{day}: failed {seeded.failed}")
        got = view_text(demo.relative_view(seeded.ledger, today, book))
        if got != want:
            wrong.append(f"{day}: {first_difference(want, got)}")
    assert not wrong, "\n".join(wrong)


def test_seed_refuses_a_directory_that_already_holds_a_ledger(tmp_path, capsys):
    c = corpus.load(corpus.default_dir())
    for unfit, why in (
        (dataclasses.replace(c, demo=()), "names no demo entries"),
        (dataclasses.replace(c, anchor=None), "names no demo anchor"),
    ):
        with pytest.raises(demo.DemoError) as err:
            demo.seed(unfit, tmp_path / "unfit", c.anchor)
        assert why in str(err.value)
        assert not (tmp_path / "unfit").exists()

    out = tmp_path / "demo"
    seeded = demo.seed(c, out, c.anchor)
    before = seeded.ledger.read_bytes()
    with pytest.raises(demo.DemoError) as err:
        demo.seed(c, out, c.anchor)
    assert str(err.value) == f"{out} already holds a ledger"
    assert seeded.ledger.read_bytes() == before
    capsys.readouterr()
    assert cli.main(["demo", "seed", "--out", str(out), "--today", "2027-06-16"]) == 2
    err_lines = capsys.readouterr().err.splitlines()
    assert f"demo: {out} already holds a ledger" in err_lines
    assert seeded.ledger.read_bytes() == before


def tree(root) -> dict[str, bytes | None]:
    """Every path under `root`, with a file's bytes (None for a directory)."""
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
        for p in sorted(root.rglob("*"))
    }


def leave_a_note(out) -> None:
    (out / "notes.txt").write_text("a note\n", encoding="utf-8")


def leave_travellers(out) -> None:
    (out / "config").mkdir()
    (out / "config" / "travellers.yaml").write_text(
        'owner:\n  names: ["Pat Example"]\n', encoding="utf-8"
    )


def leave_an_archive(out) -> None:
    (out / "archive").mkdir()
    (out / "archive" / "kept.eml").write_bytes(b"Subject: kept\n\nkept\n")


def test_seed_refuses_an_output_directory_that_is_not_empty(tmp_path, capsys):
    c = corpus.load(corpus.default_dir())
    for n, leave in enumerate((leave_a_note, leave_travellers, leave_an_archive)):
        out = tmp_path / f"out-{n}"
        out.mkdir()
        leave(out)
        before = tree(out)
        with pytest.raises(demo.DemoError) as err:
            demo.seed(c, out, c.anchor)
        assert str(err.value) == f"{out} is not empty", leave.__name__
        capsys.readouterr()
        assert cli.main(["demo", "seed", "--out", str(out), "--today", "2027-06-16"]) == 2
        assert capsys.readouterr().err.splitlines()[-1] == f"demo: {out} is not empty"
        assert tree(out) == before, leave.__name__
    empty = tmp_path / "empty"
    empty.mkdir()
    assert demo.seed(c, empty, c.anchor).failed == ()


def test_a_seed_that_stops_part_way_says_to_remove_what_it_left(tmp_path, monkeypatch, capsys):
    out = tmp_path / "demo"
    real = demo.process_raw
    calls: list[bytes] = []

    def full_at_the_second(ctx, raw, **kw):
        calls.append(raw)
        if len(calls) == 2:
            raise OSError("no space left on the device")
        return real(ctx, raw, **kw)

    monkeypatch.setattr(demo, "process_raw", full_at_the_second)
    args = ["demo", "seed", "--out", str(out), "--today", "2027-06-16"]
    capsys.readouterr()
    assert cli.main(args) == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        f"demo: cannot write {out}; remove it and seed again"
    )
    monkeypatch.setattr(demo, "process_raw", real)
    assert cli.main(args) == 2
    assert capsys.readouterr().err.splitlines()[-1] == f"demo: {out} already holds a ledger"


def test_a_today_the_calendar_cannot_hold_the_demo_for_is_refused(tmp_path, capsys):
    c = corpus.load(corpus.default_dir())
    first, last = (dt.date.fromordinal(n) for n in demo.today_range(c))
    assert first < c.anchor < last
    # the first and the last today in range seed every email
    for n, day in enumerate((first, last)):
        assert demo.seed(c, tmp_path / f"edge-{n}", day).failed == (), day
    one = dt.timedelta(days=1)
    for n, day in enumerate((first - one, last + one, dt.date.min, dt.date.max)):
        out = tmp_path / f"beyond-{n}"
        with pytest.raises(demo.DemoError) as err:
            demo.seed(c, out, day)
        assert str(err.value) == "--today is out of range"
        capsys.readouterr()
        assert cli.main(["demo", "seed", "--out", str(out), "--today", day.isoformat()]) == 2
        assert capsys.readouterr().err.splitlines()[-1] == "demo: --today is out of range"
        assert not out.exists()


def test_demo_seed_from_the_command_line(tmp_path, capsys):
    c = corpus.load(corpus.default_dir())
    out = tmp_path / "demo"
    capsys.readouterr()
    assert cli.main(["demo", "seed", "--out", str(out), "--today", "2027-06-16"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        ANCHOR_LINE,
        f"demo: ledger {out}/data/ledger.db",
        "demo: show it with TZ=America/Chicago TRIPBOX_HOME_AIRPORTS=MSP TRIPBOX_AUTH=none"
        f" TRIPBOX_DATA_DIR={out}/data TRIPBOX_CONFIG_DIR={out}/config",
    ]
    assert (out / "data" / "ledger.db").is_file()
    copied = out / "config" / "travellers.yaml"
    assert copied.read_bytes() == (c.root / corpus.TRAVELLERS).read_bytes()
    assert not (out / "data" / "ledger.db-wal").exists()

    nowhere = tmp_path / "nowhere"
    assert cli.main(["demo", "seed", "--out", str(nowhere), "--today", "tomorrow"]) == 2
    err = capsys.readouterr().err.splitlines()
    assert err and err[-1].startswith("demo: ") and "tomorrow" in err[-1]
    assert not nowhere.exists()

    # an output that cannot be written is said in one line, not as an email that was not filed
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory\n", encoding="utf-8")
    blocked = blocker / "demo"
    assert cli.main(["demo", "seed", "--out", str(blocked), "--today", "2027-06-16"]) == 2
    assert capsys.readouterr().err.splitlines()[-1] == f"demo: cannot write {blocked}"

    # without --today it is today in the corpus's zone: 03:00 UTC on 06-17 is 06-17 in UTC and in
    # the process's own zone, and still 06-16 in Chicago; the process's zone is as before after
    util.use_zone("Asia/Tokyo")
    with util.clock_at(dt.datetime(2027, 6, 17, 3, 0, tzinfo=dt.UTC)):
        assert util.today_local() == dt.date(2027, 6, 17)
        assert cli.main(["demo", "seed", "--out", str(tmp_path / "today")]) == 0
        assert os.environ["TZ"] == "Asia/Tokyo"
        assert util.today_local() == dt.date(2027, 6, 17)
    assert capsys.readouterr().out.splitlines()[0].endswith("today 2027-06-16")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a folder whatever its mode")
def test_a_seed_into_a_folder_it_cannot_write_says_so_in_one_line(tmp_path, capsys):
    # the output folder itself, and (in one that cannot be listed, which the seed lets through)
    # the folder SQLite is to make the ledger in
    out = tmp_path / "read-only"
    out.mkdir(mode=0o555)
    unlisted = tmp_path / "unlisted"
    (unlisted / "data").mkdir(parents=True)
    (unlisted / "config").mkdir()
    (unlisted / "data").chmod(0o555)
    unlisted.chmod(0o311)
    try:
        for folder in (out, unlisted):
            capsys.readouterr()
            args = ["demo", "seed", "--out", str(folder), "--today", "2027-06-16"]
            assert cli.main(args) == 2, folder
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err.splitlines() == [f"demo: cannot write {folder}"]
        assert list(out.iterdir()) == []
        assert not (unlisted / "data" / "ledger.db").exists()
    finally:
        out.chmod(0o755)
        unlisted.chmod(0o755)
        (unlisted / "data").chmod(0o755)


def test_a_corpus_named_from_the_working_directory_is_seeded(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(corpus.default_dir().parents[1])
    out = tmp_path / "demo"
    args = ["demo", "seed", "--corpus", os.path.join("tests", "corpus"), "--out", str(out)]
    capsys.readouterr()
    assert cli.main([*args, "--today", "2027-06-16"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == ANCHOR_LINE


def test_an_email_the_demo_does_not_file_is_named(tmp_path, capsys):
    c = corpus.load(corpus.default_dir())
    refused = "21-stranger"  # mail from no allowed sender: rejected, never read
    seeded = demo.seed(dataclasses.replace(c, demo=(*c.demo, refused)), tmp_path / "api", c.anchor)
    assert seeded.failed == (f"{refused}: rejected",)
    assert (seeded.emails, seeded.trips, seeded.bookings, seeded.unsorted) == (
        len(c.demo),
        7,
        22,
        1,
    )

    root = tmp_path / "corpus"
    shutil.copytree(c.root, root)
    doc = json.loads((root / "corpus.json").read_text(encoding="utf-8"))
    doc["demo"]["entries"] = sorted([*doc["demo"]["entries"], refused])
    (root / "corpus.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    listed = len(corpus.load(root).demo)
    args = ["demo", "seed", "--out", str(tmp_path / "cli"), "--today", "2027-06-16"]
    capsys.readouterr()
    assert cli.main([*args, "--corpus", str(root)]) == 1
    printed = capsys.readouterr()
    assert printed.out.splitlines()[0] == (
        f"demo: {listed - 1} emails filed into 7 trips (22 bookings, 1 unsorted), today 2027-06-16"
    )
    assert printed.err.splitlines().count(f"demo: FAILED {refused}: rejected") == 1


def test_the_demo_holds_every_kind_of_trip_the_page_shows(tmp_path):
    c = corpus.load(corpus.default_dir())
    out = tmp_path / "demo"
    seeded = demo.seed(c, out, c.anchor)
    assert seeded.failed == ()
    s = Settings.from_env(
        {
            "TZ": c.settings["TZ"],
            "TRIPBOX_HOME_AIRPORTS": c.settings["TRIPBOX_HOME_AIRPORTS"],
            "TRIPBOX_AUTH": "none",
            "TRIPBOX_DATA_DIR": str(out / "data"),
            "TRIPBOX_CONFIG_DIR": str(out / "config"),
            "TRIPBOX_LOGS_DIR": str(tmp_path / "logs"),
        }
    )
    assert s.problems == ()
    book = s.travellers
    util.use_zone(s.tz)
    with util.clock_at(dt.datetime(2027, 6, 16, 17, 0, tzinfo=dt.UTC)):
        today = util.today_local()
        assert today == c.anchor
        led = Ledger.open(seeded.ledger)
        try:
            store = TripStore(led, s.home_airports)
            assert [t.title for t in store.upcoming(today)] == ["Seattle", "Paris"]
            # a trip whose bookings are all cancelled is listed with the past ones, by its start
            assert [t.title for t in store.past(today)] == [
                "Nashville",
                "Portland",
                "Chicago",
                "Denver",
                "Boston",
            ]
            trips = {t.title: t for t in store.all_trips()}

            # a cancellation for a stay never seen: filed cancelled, in a trip of its own, to check
            assert trips["Nashville"].cancelled
            nashville = store.items(trips["Nashville"].id)
            assert [(i.kind, i.status, i.checks) for i in nashville] == [
                ("lodging", "cancelled", (checks.UNKNOWN_CANCELLATION,))
            ]

            # a flight with no date: in no trip, to check
            unsorted = store.unsorted()
            assert [(i.kind, i.trip_id, list(i.checks)) for i in unsorted] == [
                ("flight", None, ["departure"])
            ]
            # those two are the demo's marked bookings, and no other is
            assert [i.id for i in store.flagged()] == [nashville[0].id, unsorted[0].id]

            # the view the demo is held to carries each booking's open checks and the unsorted one
            view = demo.relative_view(seeded.ledger, today, book)
            marked = [
                (t["title"], i["kind"], i["checks"])
                for t in view["trips"]
                for i in t["items"]
                if i["checks"]
            ]
            assert marked == [("Nashville", "lodging", [checks.UNKNOWN_CANCELLATION])]
            assert [(u["kind"], u["status"], u["checks"]) for u in view["unsorted"]] == [
                ("flight", "confirmed", ["departure"])
            ]

            chicago = store.items(trips["Chicago"].id)
            assert [i.status for i in chicago if i.kind == "lodging"] == ["cancelled"]
            flights = [i for i in chicago if i.kind == "flight"]
            assert len(flights) == 2
            for f in flights:
                assert usual_names(book, f.passengers) == [
                    "Alex Sample",
                    "Robin Sample",
                    "Sam Sample",
                ]

            portland = store.items(trips["Portland"].id)
            assert usual_names(book, [n for i in portland for n in i.passengers]) == [
                "Lee Example",
                "Pat Example",
            ]

            # the outbound renumbered on the same day, the return as booked; the old number gone
            denver = store.items(trips["Denver"].id)
            codes = sorted(i.payload["flight_code"] for i in denver if i.kind == "flight")
            assert codes == ["DL9287", "DL9296"]
            every_flight = led.conn.execute(
                "SELECT item_key, payload_json FROM trip_items WHERE kind='flight'"
            ).fetchall()
            old = [
                r["item_key"]
                for r in every_flight
                if "|9281|" in r["item_key"] or json.loads(r["payload_json"]).get(
                    "flight_code"
                ) == "DL9281"
            ]
            assert old == []

        finally:
            led.close()

        app = create_app(s, lambda: Ledger.open(s.ledger_path), owns_ledger=True)
        # TRIPBOX_BASE_URL unset: on localhost, at the host's root
        page = TestClient(app, base_url=ORIGIN).get("/")
    assert page.status_code == 200
    for title in ("Seattle", "Paris", "Nashville", "Portland", "Chicago", "Denver", "Boston"):
        assert title in page.text, title
    # the list shows its Unsorted group, holding the flight with no date and its check
    start = page.text.index('<section id="unsorted"')
    group = page.text[start : page.text.index("</section>", start)]
    assert ">Unsorted<" in group and f'id="item-{unsorted[0].id}"' in group
    assert "To check" in group and f"<li>{escape(checks.LABELS['departure'])}</li>" in group

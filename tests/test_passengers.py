# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import os
import re
from pathlib import Path

import pytest
from helpers.sample import TRAVELLERS

from tripbox.config import ConfigError
from tripbox.validate.passengers import PassengerBook, first_last, load_travellers, name_tokens

BOOK = PassengerBook.from_yaml(TRAVELLERS)


def test_name_tokens_handles_airline_formats():
    assert name_tokens("SAMPLE/ALEX QUINN MR") == ["ALEX", "QUINN", "SAMPLE"]
    assert name_tokens("Mr. Alex Q. Sample") == ["ALEX", "Q", "SAMPLE"]
    assert name_tokens("Sample, Sam") == ["SAM", "SAMPLE"]
    assert first_last("ALEX QUINN SAMPLE") == ("ALEX", "SAMPLE")
    assert first_last("Alex") is None


def test_accents_and_apostrophes_match_a_ticket_s_plain_spelling():
    from tripbox.validate.passengers import names_match

    assert name_tokens("Renée O'Brien") == ["RENEE", "OBRIEN"]
    assert name_tokens("OBRIEN/RENEE MS") == ["RENEE", "OBRIEN"]
    assert names_match("OBRIEN/JOSE MR", "José O'Brien")
    assert names_match("O’BRIEN/JOSÉ", "Jose OBrien")
    assert names_match("SAMPLE/ZOE", "Zoë Sample")
    assert names_match("Zoë Sample", "Zoe Sample")
    assert names_match("José Sample", "Jose Sample")  # a hotel prints what the alias does not
    assert names_match("STRAUSS/ALEX", "Alex Strauß")
    assert not names_match("SAMPLE/ZOE", "Zoë Example")


def test_match_me_guest_other():
    assert BOOK.match("SAMPLE/ALEX QUINN").kind == "me"
    assert BOOK.match("Alex Sample").kind == "me"
    g = BOOK.match("SAM SAMPLE")
    assert g.kind == "guest" and g.canonical == "Sam Sample"
    o = BOOK.match("Casey Sample")
    assert o.kind == "other" and o.canonical == "Casey Sample"
    assert BOOK.match("Pat").kind == "other"


def test_classify_dedupes_and_flags_me():
    c = BOOK.classify(
        ["SAMPLE/ALEX QUINN", "Sam Sample", "SAM SAMPLE", "Max Smith", "Max Smith", ""]
    )
    assert c.me_present and c.guests == ["Sam Sample"] and c.others == ["Max Smith"]
    c2 = BOOK.classify(["Casey Sample", "Pat Example"])
    assert not c2.me_present and c2.guests == [] and len(c2.others) == 2


def test_airline_glued_first_and_middle_names_still_match():
    from tripbox.validate.passengers import PassengerBook, names_match

    book = PassengerBook.from_yaml(TRAVELLERS)
    # a middle initial may be printed glued on (SAMPLE/ALEXQ), or the whole middle name
    for printed in (
        "SAMPLE/ALEXQ",
        "SAMPLE/ALEXQUINN",
        "ALEX Q SAMPLE",
        "Sample, Alex",
    ):
        assert book.match(printed).kind == "me", printed
    for printed in ("ALEC SAMPLE", "SAMPLE/ALEXX", "ALEX SMITH", "Alex"):
        assert book.match(printed).kind == "other", printed
    assert book.match("SAM SAMPLE").kind == "guest"
    assert book.match("SAMM SAMPLE").kind == "other"  # no middle name on file for Sam
    assert names_match("SAMPLE/ALEXQ", "Alex Quinn Sample") is True
    assert (
        names_match("SAMPLE/ALEXQ", "Alex Sample") is False
    )  # alias has no middle to absorb Q


def test_single_token_greeting_name_is_nobody():
    from tripbox.validate.passengers import PassengerBook

    book = PassengerBook.from_yaml(TRAVELLERS)
    cls = book.classify(["Alex"])  # a first name alone, as a greeting prints it
    assert not cls.me_present and cls.guests == [] and cls.others == []
    cls = book.classify(["Casey", "Casey Sample"])
    assert cls.others == ["Casey Sample"]


def test_provider_spelling_does_not_change_hotel_or_car_keys():
    from tripbox.parse.models import Reservation
    from tripbox.validate.keys import real_key
    from tripbox.validate.refdata import Airports

    def car_key(res):
        return real_key("car", res.model_dump(), airports=Airports())

    def lodging_key(res):
        return real_key("lodging", res.model_dump(), airports=Airports())

    a = Reservation(kind="car", provider="Example Cars", confirmation_code="12345678QT9")
    b = Reservation(kind="car", provider="Example Rent A Car", confirmation_code="12345678qt9")
    assert car_key(a) == car_key(b) == "car|12345678QT9"
    h1 = Reservation(
        kind="lodging",
        provider="Example Hotels",
        property_name="Example Inn",
        confirmation_code="12345678",
    )
    h2 = Reservation(
        kind="lodging",
        provider="Example Inn by Example Hotels",
        property_name="Example Inn",
        confirmation_code="12345678",
    )
    assert lodging_key(h1) == lodging_key(h2) == "lodging|12345678"
    assert (
        lodging_key(Reservation(kind="lodging", property_name="Example Inn", check_in="2027-11-02"))
        == "lodging|exampleinn|2027-11-02"
    )


def test_the_shipped_travellers_example_is_the_sample_family():
    example = Path(__file__).parent.parent / "config" / "travellers.example.yaml"
    book = PassengerBook.from_yaml(example)
    assert book.me_name == "Alex Sample"
    assert book.me_aliases == ("Alex Sample", "Alex Quinn Sample", "Alex Q Sample")
    assert tuple(name for name, _ in book.guests) == ("Sam Sample", "Robin Sample", "Jamie Sample")
    assert book == PassengerBook.from_yaml(TRAVELLERS)


def test_no_travellers_file_means_nobody_is_the_owner(tmp_path):
    book, problems = load_travellers(tmp_path / "travellers.yaml")
    assert problems == () and book == PassengerBook.empty()
    cls = book.classify(["Alex Sample"])
    assert cls.me_present is False and cls.others == ["Alex Sample"] and cls.guests == []
    assert book.me_name == ""
    # an empty owner matches nobody, however a name is printed
    for printed in ("Alex Sample", "SAMPLE/ALEXQ", "", " ", "Alex"):
        assert book.match(printed).kind == "other", printed
    assert not book.classify(["", "Alex", "SAMPLE/ALEX QUINN"]).me_present
    # a file that is empty or holds only comments names nobody and is no problem
    for text in ("", "# nobody yet\n", "companions:\n"):
        (tmp_path / "travellers.yaml").write_text(text)
        assert load_travellers(tmp_path / "travellers.yaml") == (PassengerBook.empty(), ()), text
    # a file with companions and no owner: the companions are known, nobody is the owner
    (tmp_path / "travellers.yaml").write_text("companions:\n  - name: Sam Sample\n")
    book, problems = load_travellers(tmp_path / "travellers.yaml")
    assert problems == () and book.me_name == ""
    assert book.guests == (("Sam Sample", ("Sam Sample",)),)
    cls = book.classify(["Alex Sample", "Sam Sample"])
    assert not cls.me_present and cls.guests == ["Sam Sample"] and cls.others == ["Alex Sample"]


def test_every_travellers_problem_is_collected(tmp_path):
    path = tmp_path / "travellers.yaml"
    path.write_text(
        "me:\n"
        "  aliases: [Alex Sample]\n"
        "owner:\n"
        "  names: [Alex, Sam Sample]\n"
        "companions:\n"
        "  - name: Sam Sample\n"
        "  - names: [Robin Sample]\n"
    )
    book, problems = load_travellers(path)
    assert book == PassengerBook.empty()
    assert [str(p) for p in problems] == [
        "travellers.yaml: unknown key 'me' — the file holds owner: and companions:",
        "travellers.yaml: 'Alex' has one word — write each name as printed on a ticket, first"
        " and last",
        "travellers.yaml: companion 2 has no name — give each companion a name:",
        "travellers.yaml: 'Sam Sample' is listed twice — each name belongs to one person",
    ]
    assert {(p.key, p.feature) for p in problems} == {("travellers.yaml", "core")}
    with pytest.raises(ConfigError, match="unknown key 'me'"):
        PassengerBook.from_yaml(path)
    # not YAML: the line, and nothing of the file
    path.write_text("owner:\n  names: [Alex Sample\ncompanions: S3CRET-MARK\n")
    [p] = load_travellers(path)[1]
    assert re.fullmatch(
        r"travellers\.yaml: not valid YAML \(line \d+\) — fix the file or remove it", str(p)
    )
    # the old shape under the new name is not read: each old key is a problem
    path.write_text("me:\n  aliases: [Alex Sample]\nguests:\n  - name: Sam Sample\n")
    book, problems = load_travellers(path)
    assert book == PassengerBook.empty()
    assert [p.what for p in problems] == ["unknown key 'me'", "unknown key 'guests'"]
    # half converted: the old aliases: under the new keys is not read either
    path.write_text(
        "owner:\n  aliases: [Alex Sample]\ncompanions:\n  - name: Sam Sample\n    aliases: [x]\n"
    )
    book, problems = load_travellers(path)
    assert book == PassengerBook.empty()
    assert [p.what for p in problems] == [
        "unknown key 'aliases' under owner",
        "owner names: is not a list of names",
        "companion 1 has an unknown key 'aliases'",
    ]
    # a top level that is no mapping, and an owner whose names are no list of names
    for text in ("- Alex Sample\n", "owner:\n  names: Alex Sample\n", "owner:\n  names: []\n"):
        path.write_text(text)
        book, problems = load_travellers(path)
        assert book == PassengerBook.empty() and len(problems) == 1, text


def test_two_travellers_who_read_as_one_person_are_a_problem(tmp_path):
    """The matcher compares first and last names only, so two people whose names differ only by
    a middle name would be one person to it: the owner is checked first, and a companion's own
    flights would be taken for the owner's."""
    path = tmp_path / "travellers.yaml"
    one_person = (
        "read as one person (same first and last name) — tripbox cannot tell two travellers"
        " apart by a middle name; list only one of them"
    )
    for text, first, second in (
        (
            "owner:\n  names: [Alex Quinn Sample]\ncompanions:\n  - name: Alex Robin Sample\n",
            "Alex Quinn Sample",
            "Alex Robin Sample",
        ),
        (
            "owner:\n  names: [Alex Sample]\ncompanions:\n  - name: Alex Q Sample\n",
            "Alex Sample",
            "Alex Q Sample",
        ),
        (
            "companions:\n  - name: Sam Sample\n  - name: Sam Q Sample\n",
            "Sam Sample",
            "Sam Q Sample",
        ),
    ):
        path.write_text(text)
        book, problems = load_travellers(path)
        assert book == PassengerBook.empty() and book.me_name == "", text
        assert [str(p) for p in problems] == [
            f"travellers.yaml: '{first}' and '{second}' {one_person}"
        ], text
        assert {(p.key, p.feature) for p in problems} == {("travellers.yaml", "core")}
    # one problem per pair of people, however many of their names collide
    path.write_text(
        "owner:\n  names: [Alex Sample, Alex Q Sample]\n"
        "companions:\n  - name: Alex Robin Sample\n    names: [Alex Robin Sample, Alex R Sample]\n"
    )
    assert len(load_travellers(path)[1]) == 1
    # an exact duplicate keeps its own text
    path.write_text("owner:\n  names: [Sam Sample]\ncompanions:\n  - name: Sam Sample\n")
    assert [p.what for p in load_travellers(path)[1]] == ["'Sam Sample' is listed twice"]
    # one person's own names may match each other: that is what they are for
    path.write_text("owner:\n  names: [Alex Sample, Alex Quinn Sample, Alex Q Sample]\n")
    book, problems = load_travellers(path)
    assert problems == () and book.me_name == "Alex Sample"


UNREADABLE = (
    "travellers.yaml: cannot be read — it must be a regular file of at most 64 KiB that tripbox"
    " may read"
)


def test_a_directory_at_the_travellers_path_leaves_no_descriptor_open(tmp_path):
    path = tmp_path / "travellers.yaml"
    path.mkdir()
    before = len(os.listdir("/proc/self/fd"))
    results = [load_travellers(path)[1] for _ in range(5)]
    assert len(os.listdir("/proc/self/fd")) == before
    assert all([str(p) for p in problems] == [UNREADABLE] for problems in results)


def test_the_travellers_loader_never_raises(tmp_path):
    """Settings.from_env (and so doctor) reads this file: whatever is there is a problem at
    worst, never an exception and never an unbounded read."""
    path = tmp_path / "travellers.yaml"
    path.write_text("owner:\n  names: [Alex Sample]\n" + "# padding\n" * 7_200)  # about 70 KiB
    assert path.stat().st_size > 64 * 1024
    book, problems = load_travellers(path)
    assert book == PassengerBook.empty() and [str(p) for p in problems] == [UNREADABLE]
    path.write_text("owner: " + "[" * 3_000 + "]" * 3_000 + "\n")  # nested too deep to parse
    book, problems = load_travellers(path)
    assert book == PassengerBook.empty()
    assert [str(p) for p in problems] == [
        "travellers.yaml: not valid YAML — fix the file or remove it"
    ]
    path.unlink()
    os.mkfifo(path)  # a reader that would block forever if it were opened for reading
    assert [str(p) for p in load_travellers(path)[1]] == [UNREADABLE]
    path.unlink()
    path.mkdir()
    assert [str(p) for p in load_travellers(path)[1]] == [UNREADABLE]


def test_a_dangling_link_is_a_problem_not_an_absent_file(tmp_path):
    from tripbox.config import Settings, report

    (tmp_path / "travellers.yaml").symlink_to(tmp_path / "not-mounted" / "travellers.yaml")
    book, problems = load_travellers(tmp_path / "travellers.yaml")
    assert book == PassengerBook.empty() and [str(p) for p in problems] == [UNREADABLE]
    s = Settings.from_env({"TRIPBOX_AUTH": "none", "TRIPBOX_CONFIG_DIR": str(tmp_path)})
    assert s.feature("core").state == "invalid"
    lines = report(s)
    assert f"{'travellers':<15}{'invalid':<9}see the problems below" in lines
    assert not any("absent" in line for line in lines)


def test_a_key_given_twice_is_a_problem(tmp_path):
    path = tmp_path / "travellers.yaml"
    path.write_text(
        "owner:\n  names: [Alex Sample]\ncompanions:\n  - name: Sam Sample\n"
        "owner:\n  names: [Robin Sample]\n"
    )
    book, problems = load_travellers(path)
    assert book == PassengerBook.empty()
    assert [str(p) for p in problems] == ["travellers.yaml: key 'owner' appears twice — keep one"]
    path.write_text("companions:\n  - name: Sam Sample\n    name: Robin Sample\n")
    assert [p.what for p in load_travellers(path)[1]] == ["key 'name' appears twice"]

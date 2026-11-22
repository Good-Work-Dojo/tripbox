# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Tests for tools/verify/outcome-diff.py on synthetic outcome files. Every booking, place and name
is invented, and every run checks that none of them reaches standard output."""
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path

import pytest

DIFF = Path(__file__).resolve().parent / "outcome-diff.py"
KEY = bytes(range(32))


def label(value: str) -> str:
    return hmac.new(KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()[:12]


# ----------------------------------------------------------------------------- outcome-diff.py
def diff(tmp_path: Path, base: dict, head: dict, *args: str, rules: str | None = None):
    (tmp_path / "base.json").write_text(json.dumps(base))
    (tmp_path / "head.json").write_text(json.dumps(head))
    extra = list(args)
    if rules is not None:
        (tmp_path / "declared.tsv").write_text(rules)
        extra += ["--declared", str(tmp_path / "declared.tsv")]
    p = subprocess.run(
        [sys.executable, "-I", str(DIFF), str(tmp_path / "base.json"), str(tmp_path / "head.json"),
         *extra],
        capture_output=True, text=True, timeout=60,
    )
    for value in ('"airtrail"', '"tripbox"', "airtrail", "tripbox", "date_past"):
        assert value not in p.stdout, "a value was printed"
    return p


def sample_outcome() -> dict:
    return {
        "schema": 1,
        "messages": [
            {"n": 1, "status": "processed", "parser": "haiku", "document_type": "booking",
             "items": [{"kind": "flight", "key": label("a"), "route": "airtrail",
                        "action": "create_flight", "status": "written",
                        "people": {"me": 1, "guests": 0, "others": 0}, "problems": [],
                        "trip": 1}]},
            {"n": 2, "status": "processed", "parser": "kitinerary", "document_type": "booking",
             "items": []},
        ],
        "trips": [{"ordinal": 1, "items": 1, "days": 3, "title": label("t")}],
    }


def changed_route() -> dict:
    head = sample_outcome()
    head["messages"][0]["items"][0]["route"] = "tripbox"
    return head


def test_diff_identical_passes_on_equal_files_and_fails_on_one_changed_route(tmp_path):
    same = diff(tmp_path, sample_outcome(), sample_outcome(), "--expect", "identical")
    assert same.returncode == 0 and same.stdout == "differences: 0 covered: 0 uncovered: 0\n"
    p = diff(tmp_path, sample_outcome(), changed_route(), "--expect", "identical")
    assert p.returncode == 1
    assert p.stdout == "messages.0.items.0.route\ndifferences: 1 covered: 0 uncovered: 1\n"


def test_diff_refactor_passes_only_when_a_declared_rule_covers_the_change(tmp_path):
    covered = 'p2.3\tmessages.*.items.*.route\t"airtrail"\t"tripbox"\towner flights move\n'
    p = diff(tmp_path, sample_outcome(), changed_route(), "--expect", "refactor", rules=covered)
    assert p.returncode == 0 and p.stdout == "differences: 1 covered: 1 uncovered: 0\n"
    deep = "# any route anywhere\np2.3\t**.route\t*\t*\tanything goes\n"
    p = diff(tmp_path, sample_outcome(), changed_route(), "--expect", "refactor", rules=deep)
    assert p.returncode == 0
    wrong = 'p2.3\tmessages.*.items.*.route\t"review"\t"tripbox"\tnot this one\n'
    p = diff(tmp_path, sample_outcome(), changed_route(), "--expect", "refactor", rules=wrong)
    assert p.returncode == 1
    assert p.stdout == "messages.0.items.0.route\ndifferences: 1 covered: 0 uncovered: 1\n"
    p = diff(tmp_path, sample_outcome(), changed_route(), "--expect", "refactor")
    assert p.returncode == 1


def test_diff_a_path_on_one_side_only_is_matched_by_absent(tmp_path):
    head = sample_outcome()
    head["messages"][0]["items"][0]["problems"] = ["date_past"]
    rule = 'p4\tmessages.*.items.*.problems.*\tabsent\t"date_past"\tnew check\n'
    p = diff(tmp_path, sample_outcome(), head, "--expect", "refactor", rules=rule)
    assert p.returncode == 0 and p.stdout == "differences: 1 covered: 1 uncovered: 0\n"
    anything = "p4\tmessages.*.items.*.problems.*\t*\t*\tstar is not absence\n"
    p = diff(tmp_path, sample_outcome(), head, "--expect", "refactor", rules=anything)
    assert p.returncode == 1 and p.stdout.startswith("messages.0.items.0.problems.0\n")
    # the other way round: a list that empties is one difference, its entry gone
    gone = 'p4\tmessages.*.items.*.problems.*\t"date_past"\tabsent\tcheck dropped\n'
    p = diff(tmp_path, head, sample_outcome(), "--expect", "refactor", rules=gone)
    assert p.returncode == 0 and p.stdout == "differences: 1 covered: 1 uncovered: 0\n"


def test_diff_globs_reach_into_attached_emails(tmp_path):
    def with_child(key_label: str) -> dict:
        doc = sample_outcome()
        child = {"status": "processed", "parser": "kitinerary", "document_type": "booking",
                 "items": [dict(doc["messages"][0]["items"][0], key=key_label)]}
        doc["messages"][1]["children"] = [child]
        return doc

    base, head = with_child(label("old")), with_child(label("new"))
    nested = "p3\tmessages.*.children.*.items.*.key\t*\t*\tkeys rebuilt\n"
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=nested)
    assert p.returncode == 0 and p.stdout == "differences: 1 covered: 1 uncovered: 0\n"
    deep = "p3\t**.key\t*\t*\tkeys rebuilt\n"
    assert diff(tmp_path, base, head, "--expect", "refactor", rules=deep).returncode == 0
    top_only = "p3\tmessages.*.items.*.key\t*\t*\ttop-level keys only\n"
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=top_only)
    assert p.returncode == 1
    assert p.stdout == (
        "messages.1.children.0.items.0.key\ndifferences: 1 covered: 0 uncovered: 1\n"
    )
    for value in (label("old"), label("new")):
        assert value not in p.stdout


def test_diff_sees_a_change_of_kind_and_refuses_ambiguous_keys(tmp_path):
    # an object with the key "0" and a one-element list flatten to the same paths
    one = [{"n": 1}]
    p = diff(tmp_path, {"messages": one, "x": {"0": 5}}, {"messages": one, "x": [5]},
             "--expect", "identical")
    assert p.returncode == 1 and p.stdout == "x\ndifferences: 1 covered: 0 uncovered: 1\n"
    p = diff(tmp_path, {"messages": one, "x": 1}, {"messages": one, "x": [1]},
             "--expect", "identical")
    assert p.returncode == 1 and p.stdout == "x\nx.0\ndifferences: 2 covered: 0 uncovered: 2\n"
    p = diff(tmp_path, {"messages": one, "x": {}}, {"messages": one, "x": []},
             "--expect", "identical")
    assert p.returncode == 1 and p.stdout.startswith("x\n")
    # a key with a dot, or an empty one, would let two documents share paths
    for doc in ({"messages": [], "a.b": 1}, {"messages": [], "": 1}):
        p = diff(tmp_path, doc, {"messages": [], "a": {"b": 1}}, "--expect", "identical")
        assert p.returncode == 2 and "dot" in p.stderr and p.stdout == ""


def test_diff_differing_message_counts_exit_3(tmp_path):
    head = sample_outcome()
    head["messages"].pop()
    for mode in ("identical", "refactor"):
        assert diff(tmp_path, sample_outcome(), head, "--expect", mode).returncode == 3


def test_diff_usage_errors_exit_2(tmp_path):
    base, head = sample_outcome(), changed_route()
    assert diff(tmp_path, base, head, "--expect", "sideways").returncode == 2
    bad_rule = "p1\tmessages.it*ms\t*\t*\treason\n"
    assert diff(tmp_path, base, head, "--expect", "refactor", rules=bad_rule).returncode == 2
    bad_value = "p1\t**.route\tnot-json\t*\treason\n"
    assert diff(tmp_path, base, head, "--expect", "refactor", rules=bad_value).returncode == 2
    short = "p1\t**.route\t*\n"
    assert diff(tmp_path, base, head, "--expect", "refactor", rules=short).returncode == 2
    p = diff(tmp_path, base, head, "--expect", "identical", rules="p1\t**\t*\t*\tr\n")
    assert p.returncode == 2
    (tmp_path / "head.json").write_text("{not json")
    p = subprocess.run([sys.executable, "-I", str(DIFF), str(tmp_path / "base.json"),
                        str(tmp_path / "head.json"), "--expect", "identical"],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 2


def test_diff_refuses_two_outcomes_without_messages(tmp_path):
    empty = {"schema": 1, "messages": [], "trips": []}
    for mode in ("identical", "refactor"):
        p = diff(tmp_path, empty, empty, "--expect", mode)
        assert p.returncode == 2 and "no messages" in p.stderr and p.stdout == ""
    # one empty side is a different message count, as before
    assert diff(tmp_path, empty, sample_outcome(), "--expect", "identical").returncode == 3


@pytest.mark.parametrize("glob", ["*", "**", "*.*", "**.*", "*.**"])
def test_diff_refuses_a_rule_whose_glob_names_no_field(tmp_path, glob):
    rule = f"p1\t{glob}\t*\t*\tcovers everything\n"
    p = diff(tmp_path, sample_outcome(), changed_route(), "--expect", "refactor", rules=rule)
    assert p.returncode == 2 and "covers any path" in p.stderr and p.stdout == ""


# ------------------------------------------------- outcome-diff.py: the earlier item shape, mapped
# A baseline item of the earlier shape (route, action, status, problems) mapped onto the current
# one (result, sync, checks): the owner's flight, filed and sent; a copy of a booking, whose state
# is whichever the booking had; and one problem code renamed.
MAPPING = (
    "# item lines, then a check line\n"
    'p4\t@item\t{"action":"create_flight","status":"written"}\t{"result":"filed","sync":"synced"}'
    "\tfiled and sent in one pass\n"
    'p4\t@item\t{"action":"duplicate"}\t{"result":"duplicate","sync":["off","synced"]}'
    "\twhichever state the booking had\n"
    'p4\t@check\t"passengers"\t"owner_unknown"\tcode renamed\n'
)
NO_DIFFERENCE = "differences: 0 covered: 0 uncovered: 0\n"


def old_shape() -> dict:
    """sample_outcome() with a second owner's flight, filed by the second email."""
    doc = sample_outcome()
    doc["messages"][1]["items"] = [dict(doc["messages"][0]["items"][0], key=label("b"))]
    return doc


def current(doc: dict, result: str = "filed", sync: str | None = "synced") -> dict:
    """`doc` in the current shape: each item's route, action and status give way to a result and
    a sync state, and its problems become its checks, codes unchanged."""
    out = json.loads(json.dumps(doc))
    out["schema"] = 2

    def walk(m: dict) -> None:
        for x in m["items"]:
            for name in ("route", "action", "status"):
                x.pop(name)
            x.update(result=result, sync=sync, checks=x.pop("problems"))
        for child in m.get("children", []):
            walk(child)

    for m in out["messages"]:
        walk(m)
    return out


def attached(*items: dict) -> list[dict]:
    return [{"status": "ignored", "parser": "kitinerary", "document_type": "booking",
             "items": list(items)}]


def test_diff_upgrades_a_baseline_item_through_its_mapping_line(tmp_path):
    base = old_shape()
    copy = dict(base["messages"][0]["items"][0], kind="lodging", key=label("c"), route="tripbox",
                action="duplicate", status="duplicate")
    base["messages"][1]["children"] = attached(copy)  # an attached email's item is mapped too
    head = current(base)
    head["messages"][1]["children"][0]["items"][0].update(result="duplicate", sync="off")
    written = json.dumps(base)
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING)
    assert p.returncode == 0, p.stdout
    assert p.stdout == NO_DIFFERENCE
    # the base is mapped where it is compared, never in its file
    assert (tmp_path / "base.json").read_text() == written
    # the first line that matches decides: a later one that matches too is never read, and put
    # first it decides instead
    later = 'p4\t@item\t{"route":"airtrail"}\t{"result":"updated","sync":"off"}\ttoo late\n'
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING + later)
    assert p.returncode == 0 and p.stdout == NO_DIFFERENCE
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=later + MAPPING)
    assert p.returncode == 1
    assert p.stdout == "".join(f"messages.{n}.items.0.{f}\n" for n in (0, 1)
                               for f in ("result", "sync")) + (
        "differences: 4 covered: 0 uncovered: 4\n"
    )
    # a base already in the current shape is compared as it is: nothing in it is mapped
    p = diff(tmp_path, head, head, "--expect", "refactor", rules=MAPPING)
    assert p.returncode == 0 and p.stdout == NO_DIFFERENCE


def test_diff_under_the_mapping_still_sees_a_lost_key_a_changed_trip_and_changed_people(tmp_path):
    base = old_shape()
    for path, change in (
        ("messages.1.items.0.key", lambda x: x.update(key=label("other"))),
        ("messages.1.items.0.trip", lambda x: x.update(trip=2)),
        ("messages.1.items.0.people.guests", lambda x: x["people"].update(guests=1)),
    ):
        head = current(base)
        change(head["messages"][1]["items"][0])
        p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING)
        assert p.returncode == 1, path
        assert p.stdout == f"{path}\ndifferences: 1 covered: 0 uncovered: 1\n"
    # a booking the head does not hold at all: every field the mapped item has is missing
    head = current(base)
    head["messages"][1]["items"] = []
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING)
    fields = ("checks", "key", "kind", "people.guests", "people.me", "people.others", "result",
              "sync", "trip")
    assert p.returncode == 1
    assert p.stdout == "".join(f"messages.1.items.0.{f}\n" for f in fields) + (
        "differences: 9 covered: 0 uncovered: 9\n"
    )
    # and a booking the head holds that the base did not: every field of it is new
    head = current(base)
    head["messages"][1]["items"].append(dict(head["messages"][1]["items"][0], key=label("new")))
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING)
    assert p.returncode == 1
    assert p.stdout == "".join(f"messages.1.items.1.{f}\n" for f in fields) + (
        "differences: 9 covered: 0 uncovered: 9\n"
    )


def test_diff_a_mapped_flight_that_is_not_synced_is_a_difference(tmp_path):
    base = old_shape()
    head = current(base)
    head["messages"][1]["items"][0]["sync"] = "blocked"
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING)
    assert p.returncode == 1
    assert p.stdout == "messages.1.items.0.sync\ndifferences: 1 covered: 0 uncovered: 1\n"
    # after the mapping the file's ordinary rules apply, to the mapped value
    rule = 'p4\tmessages.*.items.*.sync\t"synced"\t"blocked"\ta flight held back\n'
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING + rule)
    assert p.returncode == 0 and p.stdout == "differences: 1 covered: 1 uncovered: 0\n"


def test_diff_a_baseline_item_with_no_mapping_line_fails_by_name(tmp_path):
    no_mapping = "messages.1.items.0: no mapping\ndifferences: 1 covered: 0 uncovered: 1\n"
    base = old_shape()
    head = current(base)
    base["messages"][1]["items"][0].update(route="tripbox", action="trip_create")
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING)
    assert p.returncode == 1 and p.stdout == no_mapping
    # every field a line names must match: the right action with another status is not mapped
    base = old_shape()
    base["messages"][1]["items"][0]["status"] = "review"
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING)
    assert p.returncode == 1 and p.stdout == no_mapping
    # no ordinary rule covers it, however wide
    wide = "p4\tmessages.**\t*\t*\tanything in a message\n"
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING + wide)
    assert p.returncode == 1 and p.stdout == no_mapping
    # an attached email's item is named by its own path
    base = old_shape()
    base["messages"][1]["children"] = attached(
        dict(base["messages"][0]["items"][0], action="ignore", status="ignored"))
    p = diff(tmp_path, base, current(base), "--expect", "refactor", rules=MAPPING)
    assert p.returncode == 1
    assert p.stdout == (
        "messages.1.children.0.items.0: no mapping\ndifferences: 1 covered: 0 uncovered: 1\n"
    )


def test_diff_an_either_or_takes_the_heads_value_only_from_the_list(tmp_path):
    base = old_shape()
    base["messages"][1]["items"][0].update(route="tripbox", action="duplicate",
                                           status="duplicate")

    def head_with(**fields) -> dict:
        head = current(base)
        head["messages"][1]["items"][0].update(result="duplicate", **fields)
        return head

    for sync in ("off", "synced"):  # either one is the booking's state
        p = diff(tmp_path, base, head_with(sync=sync), "--expect", "refactor", rules=MAPPING)
        assert p.returncode == 0 and p.stdout == NO_DIFFERENCE, sync
    # any other value is not taken from the head: the base holds the list's first, and differs
    for sync in ("blocked", "pending", None):
        p = diff(tmp_path, base, head_with(sync=sync), "--expect", "refactor", rules=MAPPING)
        assert p.returncode == 1, sync
        assert p.stdout == "messages.1.items.0.sync\ndifferences: 1 covered: 0 uncovered: 1\n"
    first = 'p4\tmessages.*.items.*.sync\t"off"\t"blocked"\tfrom the first\n'
    p = diff(tmp_path, base, head_with(sync="blocked"), "--expect", "refactor",
             rules=MAPPING + first)
    assert p.returncode == 0
    second = 'p4\tmessages.*.items.*.sync\t"synced"\t"blocked"\tfrom the second\n'
    p = diff(tmp_path, base, head_with(sync="blocked"), "--expect", "refactor",
             rules=MAPPING + second)
    assert p.returncode == 1
    # a result may be a list too
    either = ('p4\t@item\t{"action":"trip_create"}\t{"result":["filed","updated"],"sync":"off"}'
              "\tnew or changed\n")
    base["messages"][1]["items"][0].update(action="trip_create", status="written")
    for result, rc in (("updated", 0), ("filed", 0), ("ignored", 1)):
        head = current(base)
        head["messages"][1]["items"][0].update(result=result, sync="off")
        p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING + either)
        assert p.returncode == rc, result
    assert p.stdout == "messages.1.items.0.result\ndifferences: 1 covered: 0 uncovered: 1\n"


def test_diff_renames_problem_codes_through_the_check_lines(tmp_path):
    base = old_shape()
    base["messages"][1]["items"][0]["problems"] = [
        "confidence", "date_past", "passengers", "property", "provider",
    ]
    head = current(base)
    head["messages"][1]["items"][0]["checks"] = [
        "date_past", "low_confidence", "missing_name", "owner_unknown",
    ]
    names = (
        'p4\t@check\t"property"\t"missing_name"\tcode renamed\n'
        'p4\t@check\t"provider"\t"missing_name"\tcode renamed\n'
    )
    confidence = 'p4\t@check\t"confidence"\t"low_confidence"\tcode renamed\n'
    # renamed, each code once, sorted, and a code without a line passes as it is; a line that
    # repeats one exactly is harmless (one that gives the code another name is refused)
    again = 'p4\t@check\t"passengers"\t"owner_unknown"\tsaid twice\n'
    p = diff(tmp_path, base, head, "--expect", "refactor",
             rules=MAPPING + names + confidence + again)
    assert p.returncode == 0 and p.stdout == NO_DIFFERENCE
    # without its line a code keeps its old name, and the lists differ where it sorts
    p = diff(tmp_path, base, head, "--expect", "refactor", rules=MAPPING + names)
    assert p.returncode == 1
    assert p.stdout == (
        "messages.1.items.0.checks.0\nmessages.1.items.0.checks.1\n"
        "differences: 2 covered: 0 uncovered: 2\n"
    )


def test_diff_without_mapping_lines_behaves_as_before(tmp_path):
    base = sample_outcome()
    base["messages"][0]["items"][0]["problems"] = ["passengers"]
    head = current(base)
    head["messages"][0]["items"][0]["checks"] = ["owner_unknown"]
    fields = ("action", "checks.0", "problems.0", "result", "route", "status", "sync")
    every = "".join(f"messages.0.items.0.{f}\n" for f in fields) + "schema\n"
    expected = every + "differences: 8 covered: 0 uncovered: 8\n"
    # nothing is mapped without an @item line: not when identical is expected, not by ordinary
    # rules and not by check lines alone
    for args, rules in (
        (("--expect", "identical"), None),
        (("--expect", "refactor"), None),
        (("--expect", "refactor"), "p2.3\ttrips.*.title\t*\t*\ttitles change\n"),
        (("--expect", "refactor"), 'p4\t@check\t"passengers"\t"owner_unknown"\tcode renamed\n'),
    ):
        p = diff(tmp_path, base, head, *args, rules=rules)
        assert p.returncode == 1 and p.stdout == expected, (args, rules)


CHECK_LINE = 'p4\t@check\t"passengers"\t"owner_unknown"\tcode renamed'
MALFORMED = [
    ("from-not-json", "@item", "not-json", '{"result":"filed","sync":"off"}'),
    ("to-not-an-object", "@item", '{"action":"x"}', '["filed"]'),
    ("from-unknown-field", "@item", '{"kind":"flight"}', '{"result":"filed","sync":"off"}'),
    ("to-unknown-field", "@item", '{"action":"x"}', '{"result":"filed","sync":"off","trip":1}'),
    ("to-without-result", "@item", '{"action":"x"}', '{"sync":"off"}'),
    ("to-without-sync", "@item", '{"action":"x"}', '{"result":"filed"}'),
    ("from-names-nothing", "@item", "{}", '{"result":"filed","sync":"off"}'),
    ("from-not-a-string", "@item", '{"action":1}', '{"result":"filed","sync":"off"}'),
    ("to-an-empty-list", "@item", '{"action":"x"}', '{"result":[],"sync":"off"}'),
    ("to-null", "@item", '{"action":"x"}', '{"result":"filed","sync":null}'),
    ("to-a-list-with-a-number", "@item", '{"action":"x"}', '{"result":"filed","sync":["off",2]}'),
    ("check-not-json", "@check", "passengers", '"owner_unknown"'),
    ("check-not-a-string", "@check", '"passengers"', '["owner_unknown"]'),
    # line 1 gives the code its name, so line 2 gives it a second one
    ("check-renamed-twice", "@check", '"passengers"', '"missing_name"', CHECK_LINE),
    ("no-such-kind", "@items", '{"action":"x"}', '{"result":"filed","sync":"off"}'),
]


@pytest.mark.parametrize("kind,before,after,first",
                         [(*m[1:4], m[4] if len(m) > 4 else "# a comment, then the line")
                          for m in MALFORMED],
                         ids=[m[0] for m in MALFORMED])
def test_diff_refuses_a_malformed_mapping_line(tmp_path, kind, before, after, first):
    rules = f"{first}\np4\t{kind}\t{before}\t{after}\treason\n"
    p = diff(tmp_path, old_shape(), current(old_shape()), "--expect", "refactor", rules=rules)
    assert p.returncode == 2 and p.stdout == ""
    # named by its number and its kind of line
    assert p.stderr.startswith("outcome-diff.py: declared line 2: ") and "@" in p.stderr
    if first == CHECK_LINE:  # and by the code given two names
        assert p.stderr == (
            'outcome-diff.py: declared line 2: an @check line gives "passengers" a second'
            " check code\n"
        )

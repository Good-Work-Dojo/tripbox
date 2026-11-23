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

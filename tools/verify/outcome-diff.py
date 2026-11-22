#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""Compare two rehearsal outcome files structurally, under a policy.

Usage: outcome-diff.py <base.json> <head.json> --expect identical|refactor [--declared FILE]

Paths join object keys and list indices with dots (`messages.3.items.0.route`); every value in a
document has one, and its kind: object, array or scalar. A difference is a path present on both
sides whose kinds differ (an object that became an array, a scalar that became a list ...) or
whose scalars differ, or a path present on one side only that ends at a scalar or an empty object
or array (a non-empty one is described by its entries, which are on that side only too). An object
key that is empty or holds a dot would make two documents share paths, so it is refused.

--expect identical   any difference fails.
--expect refactor    every difference must be covered by a rule in the declared file (no file
                     means no rules). A line is `task<TAB>path-glob<TAB>from<TAB>to<TAB>reason`;
                     blank lines and lines starting with # are skipped. In the glob, `*` matches
                     exactly one path segment and `**` any number of them, zero included; every
                     other segment matches itself. `from` and `to` are JSON literals ("airtrail",
                     null, 3, []), a bare `*` for any value that is present, or the bare word
                     `absent` for a path the base (from) or the head (to) does not have. A rule
                     covers a difference when its glob matches the path and both ends match. A
                     glob made only of `*` and `**` names no field and is refused.

Two more kinds of line, in the same five columns, map a base whose items have the earlier shape
(`route`, `action`, `status`, `problems`) onto the current one (`result`, `sync`, `checks`):

  task<TAB>@item<TAB>FROM<TAB>TO<TAB>reason
      FROM is a JSON object naming one or more of `route`, `action` and `status`, each with the
      string a base item must hold there (a field left out matches anything). TO is a JSON object
      giving `result` and `sync`, each a string or a non-empty list of strings (any of them).
  task<TAB>@check<TAB>"old"<TAB>"new"<TAB>reason
      a problem code (a JSON string) and the check code that replaces it. A code may be given
      one check code only: a line repeating it with another is refused (the same one again is
      harmless).

When the declared file holds an @item line and an item of the base carries `action`, the base is
mapped before it is compared (in memory; its file is left as it is). For every item under
`messages.**.items.*` the first @item line whose FROM matches decides: `route`, `action` and
`status` are removed, `result` is set to TO's (when TO lists several, to the head's value at the
same path if it is one of them, else to the first) and `sync` likewise, and `problems` becomes
`checks`, each code passed through the @check lines, each code once, sorted; the base's `schema`,
when it states one, becomes 2. `key`, `people` and `trip` are never touched, so every booking the
base holds must still be in the head with the same key, travellers and trip. An item no line
matches is one uncovered difference, printed as `<its path>: no mapping`, and nothing inside it is
compared. Then the comparison runs as above, with the file's other rules. Without an @item line,
or with a base already in the current shape, nothing is mapped.

Output: one line per uncovered difference giving its path only, never a value, then
`differences: N covered: M uncovered: K`. Exit 0 when K = 0, 1 when K > 0, 3 when the two files
hold different numbers of messages (nothing else is compared then), 2 on a usage error (a
malformed declared line is named by its number), an unreadable file, or two files that both hold
no messages (a comparison of nothing passes nothing).
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ABSENT = object()  # the path is not in that document
ANY = object()  # a rule's bare `*`: any value that is present
ITEM_FROM = ("route", "action", "status")  # what an @item line may match on a base item
ITEM_TO = ("result", "sync")  # what it sets there
ITEMS = ["messages", "**", "items", "*"]  # where an outcome's items are, attached emails' too
MAPPED_SCHEMA = 2  # the schema of an outcome whose items have the current shape


class UsageError(Exception):
    pass


def same(a: object, b: object) -> bool:
    """Equal and of the same JSON type: true is not 1, while 3 and 3.0 are one number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    return type(a) is type(b) and a == b


def kind(node: object) -> str:
    if isinstance(node, dict):
        return "object"
    return "array" if isinstance(node, list) else "scalar"


def nodes(doc: object) -> dict[str, object]:
    """Path -> value, for every value in the document (the root's path is empty)."""
    out: dict[str, object] = {}

    def walk(node: object, path: str) -> None:
        out[path] = node
        if isinstance(node, dict):
            for k, v in node.items():
                if not k or "." in k:
                    raise UsageError("an object key is empty or holds a dot")
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}.{i}" if path else str(i))

    walk(doc, "")
    return out


def path_order(path: str) -> list[tuple[int, int | str]]:
    return [(0, int(s)) if s.isdigit() else (1, s) for s in path.split(".")]


def differences(base: object, head: object) -> list[tuple[str, object, object]]:
    bn, hn = nodes(base), nodes(head)
    out = []
    for path in sorted(set(bn) | set(hn), key=path_order):
        b, h = bn.get(path, ABSENT), hn.get(path, ABSENT)
        if b is ABSENT or h is ABSENT:
            only = h if b is ABSENT else b
            if kind(only) == "scalar" or not only:  # a scalar, or an empty object or array
                out.append((path, b, h))
        elif kind(b) != kind(h) or (kind(b) == "scalar" and not same(b, h)):
            out.append((path, b, h))
    return out


@dataclass
class Rule:
    glob: list[str]
    before: object
    after: object


@dataclass
class ItemLine:
    """An @item line: the fields a base item must hold, and for `result` and `sync` the values
    it may take (the first unless the head holds another of them)."""

    match: dict[str, str]
    to: dict[str, list[str]]


@dataclass
class Declared:
    rules: list[Rule] = field(default_factory=list)
    items: list[ItemLine] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)  # problem code -> check code


def parse_end(text: str, number: int) -> object:
    if text == "*":
        return ANY
    if text == "absent":
        return ABSENT
    try:
        return json.loads(text)
    except ValueError:
        raise UsageError(f"declared line {number}: from and to are JSON, * or absent") from None


def parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return ABSENT


def parse_item(before: str, after: str, number: int) -> ItemLine:
    where = f"declared line {number}: an @item line's"
    match, to = parse_json(before), parse_json(after)
    if not isinstance(match, dict) or not isinstance(to, dict):
        raise UsageError(f"{where} from and to are JSON objects")
    if not set(match) <= set(ITEM_FROM):
        raise UsageError(f"{where} from names only route, action and status")
    if not match:
        raise UsageError(f"{where} from names no field, so it would match every item")
    if not all(isinstance(v, str) for v in match.values()):
        raise UsageError(f"{where} from gives each field a string")
    if set(to) != set(ITEM_TO):
        raise UsageError(f"{where} to gives result and sync, and nothing else")
    values = {k: v if isinstance(v, list) else [v] for k, v in to.items()}
    if not all(v and all(isinstance(s, str) for s in v) for v in values.values()):
        raise UsageError(f"{where} result and sync are each a string or a list of strings")
    return ItemLine(match, values)


def read_declared(path: Path) -> Declared:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise UsageError("cannot read the declared file") from None
    declared = Declared()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [f.strip() for f in line.split("\t")]
        if len(fields) != 5 or not all(fields):
            raise UsageError(f"declared line {number}: needs task, glob, from, to and reason")
        _task, glob, before, after, _reason = fields
        if glob == "@item":
            declared.items.append(parse_item(before, after, number))
            continue
        if glob == "@check":
            old, new = parse_json(before), parse_json(after)
            if not isinstance(old, str) or not isinstance(new, str):
                raise UsageError(f"declared line {number}: an @check line's from and to are"
                                 " JSON strings")
            if declared.checks.get(old, new) != new:  # an exact repeat is harmless
                raise UsageError(f"declared line {number}: an @check line gives {json.dumps(old)}"
                                 " a second check code")
            declared.checks[old] = new
            continue
        if glob.startswith("@"):
            raise UsageError(f"declared line {number}: a line of its own kind is @item or @check")
        segments = glob.split(".")
        if any(not s or ("*" in s and s not in ("*", "**")) for s in segments):
            raise UsageError(f"declared line {number}: a glob segment is a name, * or **")
        if all(s in ("*", "**") for s in segments):
            raise UsageError(f"declared line {number}: a glob of only * and ** covers any path")
        declared.rules.append(Rule(segments, parse_end(before, number), parse_end(after, number)))
    return declared


def glob_match(pattern: list[str], parts: list[str]) -> bool:
    if not pattern:
        return not parts
    first, rest = pattern[0], pattern[1:]
    if first == "**":
        return any(glob_match(rest, parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and first in ("*", parts[0]) and glob_match(rest, parts[1:])


def end_matches(want: object, got: object) -> bool:
    if want is ABSENT:
        return got is ABSENT
    if want is ANY:
        return got is not ABSENT
    return got is not ABSENT and same(want, got)


def covered(rules: list[Rule], path: str, base: object, head: object) -> bool:
    parts = path.split(".")
    return any(
        glob_match(r.glob, parts) and end_matches(r.before, base) and end_matches(r.after, head)
        for r in rules
    )


def map_items(base: dict, head: dict, declared: Declared) -> list[str]:
    """Rewrite the base's items of the earlier shape into the current one, in place, through the
    @item and @check lines; returns the paths of the items no @item line matches, left as they
    are. Nothing is rewritten without an @item line or when no base item carries `action`."""
    items = [
        (path, node) for path, node in nodes(base).items()
        if isinstance(node, dict) and glob_match(ITEMS, path.split("."))
    ]
    if not declared.items or not any("action" in node for _, node in items):
        return []
    heads = nodes(head)
    unmatched = []
    for path, item in items:
        line = next(
            (x for x in declared.items
             if all(k in item and same(item[k], v) for k, v in x.match.items())),
            None,
        )
        if line is None:
            unmatched.append(path)
            continue
        for name in ITEM_FROM:
            item.pop(name, None)
        for name, values in line.to.items():
            theirs = heads.get(f"{path}.{name}", ABSENT)
            item[name] = theirs if isinstance(theirs, str) and theirs in values else values[0]
        if "problems" in item:
            problems = item.pop("problems")
            if not isinstance(problems, list) or not all(isinstance(c, str) for c in problems):
                raise UsageError("the base holds an item whose problems are not a list of codes")
            item["checks"] = sorted({declared.checks.get(c, c) for c in problems})
    if "schema" in base:
        base["schema"] = MAPPED_SCHEMA
    return unmatched


def inside(path: str, top: str) -> bool:
    return path == top or path.startswith(top + ".")


def load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise UsageError(f"cannot read {path.name}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
        raise UsageError(f"{path.name} is not an outcome file (no list of messages)")
    try:
        nodes(doc)
    except UsageError as exc:
        raise UsageError(f"{path.name}: {exc}, so its paths would be ambiguous") from None
    return doc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="outcome-diff.py", description=__doc__.splitlines()[0])
    parser.add_argument("base", type=Path)
    parser.add_argument("head", type=Path)
    parser.add_argument("--expect", required=True, choices=("identical", "refactor"))
    parser.add_argument("--declared", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.declared is not None and args.expect != "refactor":
            raise UsageError("--declared applies to --expect refactor only")
        declared = read_declared(args.declared) if args.declared is not None else Declared()
        base, head = load(args.base), load(args.head)
    except UsageError as exc:
        print(f"outcome-diff.py: {exc}", file=sys.stderr)
        return 2
    if not base["messages"] and not head["messages"]:
        print("outcome-diff.py: both files hold no messages, so nothing is compared",
              file=sys.stderr)
        return 2
    if len(base["messages"]) != len(head["messages"]):
        print(f"messages: base {len(base['messages'])} head {len(head['messages'])}")
        return 3
    try:
        unmatched = map_items(base, head, declared)
    except UsageError as exc:
        print(f"outcome-diff.py: {exc}", file=sys.stderr)
        return 2
    # (path, line printed when uncovered, covered); an unmatched item stands for all inside it
    found = [
        (path, path, covered(declared.rules, path, b, h))
        for path, b, h in differences(base, head)
        if not any(inside(path, top) for top in unmatched)
    ]
    found += [(path, f"{path}: no mapping", False) for path in unmatched]
    found.sort(key=lambda f: path_order(f[0]))
    uncovered = [line for _path, line, ok in found if not ok]
    for line in uncovered:
        print(line)
    n, k = len(found), len(uncovered)
    print(f"differences: {n} covered: {n - k} uncovered: {k}")
    return 1 if k else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

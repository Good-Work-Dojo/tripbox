# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""tools/publish_gate.py: a tree to be published holds nothing from a private denylist.

Every fixture is synthetic and lives under tmp_path. Every CLI run goes through `gate` (a
subprocess) or `images` (in process, with a fake docker), and both assert that no token any test
has put in a denylist, and no part of a combo, reaches stdout, stderr or the --json file, folded or
encoded, so each failing fixture below also checks that the gate never prints what it found.
"""

import base64
import bz2
import gzip
import hashlib
import hmac
import html
import importlib.util
import io
import json
import lzma
import os
import re
import stat
import struct
import subprocess
import sys
import tarfile
import time
import tracemalloc
import unicodedata
import zipfile
import zlib
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, unquote_plus

import pytest

GATE = Path(__file__).resolve().parent / "publish_gate.py"
_spec = importlib.util.spec_from_file_location("publish_gate", GATE)
pg = importlib.util.module_from_spec(_spec)
sys.modules["publish_gate"] = pg
_spec.loader.exec_module(pg)

SEP = "\x1f"
# generic-detector fixtures are assembled from pieces, so that this file holds no address, home
# path or private network of its own
AT, PCT_AT, ENT_AT, BS, SL, PCT_SL = "@", "%40", "&#64;", "\\", "/", "%2F"
HOME, USERS = "/home", "/Users"
ADDR = f"alex{AT}samplefamily.test"
OTHER = f"bob{AT}otherplace.internal"
TOKENS = {
    "identifier": ["demo_booking_ZZTEST", "pnr9k2m4x7"],
    "name": ["Samplefamily", "Alexandria Quill", "Zoë Quillon"],
    "email": [ADDR],
    "host": ["trips.samplefamily.test"],
    "date": ["2024-03-17"],
    "vendor": ["Quuxair", "Blorbo Lodge"],
    "place": ["12 Example Way", "Tamarind Lodge"],
    "combo": [f"Robin Sample{SEP}QQ4X7P", f"Casa Zorblax{SEP}Alex Sample{SEP}ZZPNR1"],
}
COMBOS = [t.split(SEP) for t in TOKENS["combo"]]
PARTS = [p for parts in COMBOS for p in parts]
ALL_TOKENS = [t for ts in TOKENS.values() for t in ts]
# every token any test has written to a denylist, and every part of each combo
USED: set[str] = {t for t in ALL_TOKENS if SEP not in t} | set(PARTS)
ID = "demo_booking_ZZTEST"
KEY = bytes(range(32))
REDACTED = r"\[redacted:hmac:[0-9a-f]{12}\]"
# a finding line's optional `:<line>` after the path and `×<count>` at the end
DECORATION = re.compile(
    r"^((?:accepted )?[a-e] \S+ .*?)(?::\d+)?((?: hmac:[0-9a-f]{12})?)(?: ×\d+)?$")
INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\u00ad\ufeff"))


def undecorated(line: str) -> str:
    m = DECORATION.match(line)
    return m.group(1) + m.group(2) if m else line


def dotted(*octets: int) -> str:
    return ".".join(map(str, octets))


def fold(s: str) -> str:
    s = s.translate(INVISIBLE)
    s = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", s).casefold())
    return re.sub(r"\s+", " ", s)


def lab(token: str, key: bytes = KEY) -> str:
    """The label of a token; a combo's parts fold one by one, since the separator is whitespace"""
    folded = SEP.join(fold(part).strip() for part in token.split(SEP))
    return hmac.new(key, folded.encode(), hashlib.sha256).hexdigest()[:12]


def b64_cores(data: bytes) -> list[str]:
    out = []
    for align in range(3):
        enc = base64.b64encode(b"\0" * align + data).decode()
        out.append(enc[-(-8 * align // 6): 8 * (align + len(data)) // 6])
    return out


def leak_patterns(token: str) -> list[str]:
    """What printing `token` looks like, matched in folded output: its alphanumeric runs joined by
    any separators or none (bounded on both sides when the token is short), and each run of six or
    more characters of a multi-run token on its own, since printing part of a token prints part of
    the household string"""
    t = fold(token)
    runs = re.findall(r"[^\W_]+", t)
    if not runs:
        return [re.escape(t)]
    bound = r"(?<![^\W_]){}(?![^\W_])"
    joined = r"[\W_]*".join(map(re.escape, runs))
    out = [joined if len(t) >= 6 else bound.format(joined)]
    if len(runs) > 1:
        out += [bound.format(re.escape(r)) for r in runs if len(r) >= 6]
    return out


def decoded_forms(text: str) -> list[str]:
    """`text` and its percent-, \\u- and &#-decoded forms, folded, as the gate reads them"""
    forms = [text]
    if "%" in text:
        forms.append(unquote_plus(text))
    if "\\u" in text:
        forms.append(re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text))
    if "&" in text:
        forms.append(html.unescape(text))
    return [fold(f) for f in forms]


def assert_no_token(text: str) -> None:
    """No token and no part of a combo appears in `text`: not in any spelling of its runs
    (`alexandria_quill`, `AlexandriaQuill`), not in part, not behind a percent-, \\u- or
    &#-escape, and not hex-, percent- or base64-encoded."""
    readings = decoded_forms(text)
    folded = readings[0]
    for token in USED:
        t = fold(token)
        if len(t) < 3:
            continue
        for pattern in leak_patterns(token):
            for reading in readings:
                assert not re.search(pattern, reading), (
                    f"a denylist token (hmac:{lab(token)}) leaked")
        for spelling in {token, token.lower(), token.upper(), token.title()}:
            data = spelling.encode()
            encoded_folded = [data.hex()] if len(data.hex()) >= 6 else []
            if quote(spelling, safe="") != spelling:
                encoded_folded.append(fold(quote(spelling, safe="")))
            for form in encoded_folded:
                assert form not in folded, f"an encoded denylist token (hmac:{lab(token)}) leaked"
            for core in b64_cores(data):
                for form in {core, core.translate(str.maketrans("+/", "-_"))}:
                    assert len(form) < 6 or form not in text, (
                        f"a base64 token (hmac:{lab(token)}) leaked")


def write_deny(tmp_path: Path, tokens: dict | None = None, *, allow_vendors=(), allow_paths=(),
               accepted=(), allow_domains=(), allow_generic=(), raw: str | None = None,
               key: bytes | None = KEY, name: str = "deny") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    tokens = tokens or TOKENS
    for cls, ts in tokens.items():
        USED.update(parts for t in ts for parts in (t.split(SEP) if cls == "combo" else [t]))
    body = "\n".join(f"{c}\t{t}" for c, ts in tokens.items() for t in ts)
    (d / "tokens.tsv").write_text(
        raw if raw is not None else "# synthetic denylist\n\n" + body + "\n", encoding="utf-8")
    if key is not None and not (d / "hmac.key").exists():
        (d / "hmac.key").write_bytes(key)
        (d / "hmac.key").chmod(0o600)
    if allow_vendors:
        (d / "allow-vendors.txt").write_text("\n".join(allow_vendors) + "\n", encoding="utf-8")
    if allow_domains:
        (d / "allow-domains.txt").write_text("\n".join(allow_domains) + "\n", encoding="utf-8")
    for fname, rows in (("allow-paths.tsv", allow_paths), ("accepted.tsv", accepted),
                        ("allow-generic.tsv", allow_generic)):
        if rows:
            (d / fname).write_text("\n".join("\t".join(r) for r in rows) + "\n",
                                   encoding="utf-8")
    return d


def tree(tmp_path: Path, files: dict[str, str | bytes], name: str = "tree") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body, encoding="utf-8")
    return root


def zip_bytes(members: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in members.items():
            z.writestr(name, body)
    return buf.getvalue()


def tar_bytes(members: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for name, body in members.items():
            data = body.encode() if isinstance(body, str) else body
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def png_bytes(*chunks: tuple[bytes, bytes], trailing: bytes = b"") -> bytes:
    def chunk(ctype: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(ctype + data))
        return struct.pack(">I", len(data)) + ctype + data + crc
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    body = chunk(b"IHDR", ihdr) + b"".join(chunk(t, d) for t, d in chunks)
    end = chunk(b"IDAT", zlib.compress(b"\x00\x00")) + chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + body + end + trailing


class Result(NamedTuple):
    rc: int
    out: str
    err: str
    report: dict | None

    @property
    def lines(self) -> list[str]:
        """The output lines with each finding's line number and count left out"""
        return [undecorated(line) for line in self.out.splitlines()]

    @property
    def raw_lines(self) -> list[str]:
        return self.out.splitlines()


@pytest.fixture
def gate(tmp_path):
    """Run the gate as a script; scans also write --json. Asserts the return code when `rc` is
    given. `raw_ok` names the exact strings a test expects to be printed although they hold a token
    (a path the token is allowed in); only those are cut out of the output before the leak
    check."""
    counter = iter(range(1000))

    def run(*args, rc: int | None = None, stdin: str | None = None,
            raw_ok: tuple[str, ...] = ()) -> Result:
        argv = [str(a) for a in args]
        report_path = None
        if argv[0] == "scan" and "--json" not in argv:
            report_path = tmp_path / f"report-{next(counter)}.json"
            argv += ["--json", str(report_path)]
        p = subprocess.run([sys.executable, str(GATE), *argv], capture_output=True, text=True,
                           timeout=600, input=stdin)
        report_text = report_path.read_text() if report_path and report_path.exists() else ""
        for stream in (p.stdout, p.stderr, report_text):
            for allowed in raw_ok:
                stream = stream.replace(allowed, "")
            assert_no_token(stream)
        if rc is not None:
            assert p.returncode == rc, (p.returncode, p.stdout, p.stderr)
        return Result(p.returncode, p.stdout, p.stderr,
                      json.loads(report_text) if report_text else None)
    return run


def scan(gate, tmp_path, files, *, rc=None, parts=None, raw_ok=(), **deny_kw) -> Result:
    args = ["scan", "--tree", tree(tmp_path, files), "--denylist",
            write_deny(tmp_path, **deny_kw)]
    if parts:
        args += ["--parts", parts]
    return gate(*args, rc=rc, raw_ok=raw_ok)


def hit(part: str, kind: str, path: str, token: str | None = None) -> str:
    return f"{part} {kind} {path}" + (f" hmac:{lab(token)}" if token else "")


# --- the clean baseline --------------------------------------------------------------------------

CLEAN = {
    "README.md": "A demo household app. Contact alex@example.org or a@users.noreply.github.com.\n",
    "src/app.py": "print('2024-03-17', 'Robin')\nSAMPLE = 'sample'\n",
    "src/shop.ts": "export const vendor = 'Quuxair';\n",
    "src/net.py": (f"PUBLIC = '{dotted(8, 8, 8, 8)}'\nOTHER = '{dotted(172, 32, 0, 1)}'\n"
                   f"HOME = '{HOME}/'\nCI = '{HOME}/runner/work'\nAPP = '{HOME}/app'\n"
                   f"ME = '{USERS}/me/x'\nNESTED = '/opt/home/alex'\n"
                   "SECTION = 'src/sections/home/TopCards'\n"),
    "src/home.tsx": "import Top from './home/TopCards';\nimport Up from '../home/Summary';\n",
    "data/rows.csv": "date,city\n2025-01-01,Sampleton\n",
    "icons/a.png": png_bytes(),
    "dist/bundle.zip": zip_bytes({"inner/readme.txt": "nothing here\n"}),
    "app/package-lock.json": json.dumps({"packages": {"node_modules/x": {
        "version": "1.0.0", "resolved": "https://npm.samplecorp.test/x/-/x-1.0.0.tgz"}}}),
    "uv.lock": '[[package]]\nversion = "2024.3"\n'
               'source = { registry = "https://pypi.org/simple" }\n'
               'wheels = [{ url = "https://files.pythonhosted.org/x.whl" }]\n',
}


def test_a_clean_tree_exits_zero(tmp_path, gate):
    r = scan(gate, tmp_path, CLEAN, rc=0, allow_vendors=["quuxair"])
    assert r.raw_lines == ["findings: 0 (accepted: 0)"]
    assert r.report["findings"] == []
    keys = ("findings", "accepted", "occurrences", "allowed")
    assert {k: r.report["summary"][k] for k in keys} == dict.fromkeys(keys, 0)


def test_the_json_report_is_owner_only_whatever_the_umask(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("old")
    report.chmod(0o644)
    args = ["scan", "--tree", tree(tmp_path, CLEAN), "--denylist", write_deny(tmp_path),
            "--json", str(report)]
    p = subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True,
                       timeout=600, preexec_fn=lambda: os.umask(0o022))
    assert p.returncode in (0, 1), p.stderr
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert json.loads(report.read_text())["summary"]["findings"] >= 0
    fresh = tmp_path / "fresh.json"
    args[-1] = str(fresh)
    subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True,
                   timeout=600, preexec_fn=lambda: os.umask(0o022))
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_scan_tree_is_the_python_interface(tmp_path):
    t = tree(tmp_path, {"notes.md": f"id {ID}\n"})
    findings = pg.scan_tree(t, pg.Denylist.load(write_deny(tmp_path)), {"c"})
    assert findings == [pg.Finding("c", "identifier", "notes.md", lab(ID), count=1, line=1)]


# --- labels --------------------------------------------------------------------------------------

def test_labels_are_keyed_and_are_not_a_plain_hash(tmp_path):
    one = pg.Denylist.load(write_deny(tmp_path, name="one"))
    two = pg.Denylist.load(write_deny(tmp_path, name="two", key=bytes([7]) * 32))
    for token in ALL_TOKENS:
        assert one.label(token) == lab(token)
        assert one.label(token) != two.label(token)
        assert one.label(token) != hashlib.sha256(fold(token).encode()).hexdigest()[:12]


def test_a_missing_key_is_created_private_and_a_readable_one_is_refused(tmp_path, gate):
    d = write_deny(tmp_path, key=None)
    pg.Denylist.load(d)
    key = d / "hmac.key"
    assert len(key.read_bytes()) == 32 and stat.S_IMODE(key.stat().st_mode) == 0o600
    key.chmod(0o640)
    r = gate("scan", "--tree", tree(tmp_path, {"a.md": "x"}), "--denylist", d, rc=2)
    assert "hmac.key" in r.err


@pytest.mark.parametrize("size", [31, 33])
def test_a_key_of_the_wrong_length_is_refused_without_printing_it(tmp_path, gate, size):
    d = write_deny(tmp_path, key=(b"keymaterial-" * 3)[:size])
    r = gate("scan", "--tree", tree(tmp_path, {"a.md": "x"}), "--denylist", d, rc=2)
    assert "hmac.key" in r.err
    for stream in (r.out, r.err):
        assert "keymaterial" not in stream and b"keymaterial".hex() not in stream


def test_the_label_command_reads_tokens_from_stdin(tmp_path, gate):
    r = gate("label", "--denylist", write_deny(tmp_path), stdin=f"{ID}\n\n  Samplefamily \n", rc=0)
    assert r.lines == [lab(ID), lab("Samplefamily")]


@pytest.mark.parametrize("spelling", [f"Robin Sample{SEP}QQ4X7P", "Robin Sample\\x1fqq4x7p",
                                      f"  ROBIN  SAMPLE{SEP}QQ4X7P \n"])
def test_the_label_command_labels_a_combo_with_a_separator_or_its_escape(tmp_path, gate, spelling):
    r = gate("label", "--denylist", write_deny(tmp_path), stdin=spelling + "\n", rc=0)
    assert r.lines == [lab(f"Robin Sample{SEP}QQ4X7P")]
    assert r.lines != [lab("Robin Sample QQ4X7P")]      # the separator is part of the token


def test_a_usage_error_never_repeats_an_argument_value(tmp_path, gate):
    d, t = write_deny(tmp_path), tree(tmp_path, {"a.md": "x"})
    for args in (["label", "--denylist", d, "Alexandria Quill"],
                 ["scan", "--tree", t, "--denylist", d, "Samplefamily"],
                 ["scan", "--tree", t, "--denylist", d, "--parts", "Samplefamily"],
                 ["Samplefamily", "--denylist", d],
                 ["scan", "--tree", t, "--denylist", d, "--pnr9k2m4x7"]):
        r = gate(*args, rc=2)
        assert r.err.strip()


# --- (b) png text chunks -------------------------------------------------------------------------

@pytest.mark.parametrize("ctype", [b"tEXt", b"zTXt", b"iTXt", b"eXIf"])
def test_a_png_text_chunk_is_a_finding(tmp_path, gate, ctype):
    r = scan(gate, tmp_path, {"icons/a.PNG": png_bytes((ctype, b"Comment\x00hello"))}, rc=1,
             parts="b")
    assert r.lines == [hit("b", "png-text", "icons/a.PNG"), "findings: 1 (accepted: 0)"]


@pytest.mark.parametrize("body", [png_bytes()[:-6], b"not a png at all",
                                  png_bytes(trailing=b"tail")])
def test_a_malformed_png_is_a_finding(tmp_path, gate, body):
    r = scan(gate, tmp_path, {"icons/a.png": body}, rc=1, parts="b")
    assert hit("b", "png-malformed", "icons/a.png") in r.lines


# --- (c) tokens ----------------------------------------------------------------------------------

@pytest.mark.parametrize("path,body,cls,token", [
    ("notes.md", f"see {ID} here\n", "identifier", ID),
    ("src/app.py", "x = 'pnr9k2m4x7'\n", "identifier", "pnr9k2m4x7"),
    ("src/greet.py", "print('Hi Samplefamily!')\n", "name", "Samplefamily"),
    ("src/who.ts", "const who = 'ALEXANDRIA QUILL';\n", "name", "Alexandria Quill"),
    ("docs/fw.md", "\uff33\uff21\uff2d\uff30\uff2c\uff25\uff26\uff21\uff2d\uff29\uff2c\uff39\n",
     "name", "Samplefamily"),
    ("docs/mail.md", f"write to Alex{AT}SampleFamily.test today\n", "email", ADDR),
    ("config/hosts.yaml", "url: https://trips.samplefamily.test/x\n", "host",
     "trips.samplefamily.test"),
    ("src/shop.tsx", "<p>Quuxair</p>\n", "vendor", "Quuxair"),
    ("src/lodge.py", "# blorbo lodge stay\n", "vendor", "Blorbo Lodge"),
    ("src/stay.py", "# staying at 12 EXAMPLE way\n", "place", "12 Example Way"),
    ("docs/stay.md", "Tamarind\nLodge, west wing\n", "place", "Tamarind Lodge"),
    ("data/t.json", '{"d": "2024-03-17"}\n', "date", "2024-03-17"),
    ("data/ts.json", '{"at": "2024-03-17T08:15:00Z"}\n', "date", "2024-03-17"),
    ("data/t.txt", "departs 2024-03-17\n", "date", "2024-03-17"),
    ("data/t.csv", "date,city\n2024-03-17,Sampleton\n", "date", "2024-03-17"),
    ("data/t.tsv", "2024-03-17\tSampleton\n", "date", "2024-03-17"),
    ("data/t.yml", "when: 2024-03-17\n", "date", "2024-03-17"),
    ("data/t.yaml", "when: 2024-03-17\n", "date", "2024-03-17"),
    ("data/t.sql", "insert into t values ('2024-03-17');\n", "date", "2024-03-17"),
    ("data/t.eml", "Date: 2024-03-17\n", "date", "2024-03-17"),
    ("data/t.ics", "DESCRIPTION:flight on 2024-03-17\n", "date", "2024-03-17"),
])
def test_each_class_is_found_in_contents(tmp_path, gate, path, body, cls, token):
    r = scan(gate, tmp_path, {path: body}, rc=1)
    assert hit("c", cls, path, token) in r.lines


def test_a_date_is_only_searched_in_data_files(tmp_path, gate):
    files = {"src/a.py": "x = '2024-03-17'\n", "docs/a.md": "on 2024-03-17\n",
             "src/b.html": "<td>2024-03-17</td>\n", "data/other.dat": "2024-03-17\n"}
    scan(gate, tmp_path, files, rc=0)


@pytest.mark.parametrize("text,found", [
    ("on 2024-03-17", True), ("x-2024-03-17-y", True), ("(2024-03-17)", True),
    ("2024-03-17", True), ("a.2024-03-17/b", True), ("2024/03/17", True), ("2024.03.17", True),
    ("a2024-03-17", False), ("12024-03-17", False), ("2024-03-170", False),
    ("2024-03-17a", False), ("2024-03-18", False),
])
def test_a_match_needs_a_non_alphanumeric_boundary_on_both_sides(tmp_path, gate, text, found):
    r = scan(gate, tmp_path, {"data/rows.csv": f"memo\n{text}\n", "src/rows.py": f"m = '{text}'\n"})
    assert (hit("c", "date", "data/rows.csv", "2024-03-17") in r.lines) is found
    assert not [line for line in r.lines if line.startswith("c date src/rows.py")]


def test_folding_collapses_whitespace_and_drops_invisible_characters():
    assert pg.fold("Alexandria\t\r\n  QUILL") == "alexandria quill"
    assert pg.fold("Sam\u00adple\u200bfam\u200c\u200dily\u2060\ufeff") == "samplefamily"
    assert pg.fold("\uff33ample") == "sample"


@pytest.mark.parametrize("text", [
    "Alexandria\nQuill", "Alexandria  Quill", "Alexandria\tQuill", "Alexandria\r\n   Quill",
    "alexandria-quill", "alexandria_quill", "ALEXANDRIA.QUILL", "Alexandria \u2014 Quill",
])
def test_a_multi_word_token_matches_across_any_separator(tmp_path, gate, text):
    r = scan(gate, tmp_path, {"docs/a.md": f"by {text} today\n"}, rc=1)
    assert hit("c", "name", "docs/a.md", "Alexandria Quill") in r.lines


@pytest.mark.parametrize("text,token", [
    ("demo-booking-zztest", ID), ("demo booking ZZTEST", ID), ("demo.booking/zztest", ID),
    ("Ottoline O\u2019Fennick", "Ottoline O'Fennick"), ("Sample\u00adfamily", "Samplefamily"),
    ("Sample\u200bfamily", "Samplefamily"), ("Sample\u2060family", "Samplefamily"),
    ("\ufeffSamplefamily", "Samplefamily"),
])
def test_separator_and_invisible_character_variants_match(tmp_path, gate, text, token):
    tokens = {**TOKENS, "name": [*TOKENS["name"], "Ottoline O'Fennick"]}
    r = scan(gate, tmp_path, {"docs/a.md": f"x {text} y\n"}, rc=1, tokens=tokens)
    assert hit("c", "identifier" if token == ID else "name", "docs/a.md", token) in r.lines


@pytest.mark.parametrize("text", ["xalexandria quill", "alexandria quills", "demo_booking_zztests",
                                  "demobooking_zztest"])
def test_the_alphanumeric_runs_must_match_exactly(tmp_path, gate, text):
    scan(gate, tmp_path, {"docs/a.md": f"{text}\n"}, rc=0)


@pytest.mark.parametrize("token,text,found", [
    ("-Gate7", "dep,-Gate7,x", True), ("-Gate7", "x-Gate7", True), ("-Gate7", "Gate7", True),
    ("-Gate7", "-Gate70", False), ("-Gate7", "xgate7", False),
    ("Hall4-", "see Hall4-,x", True), ("Hall4-", "Hall4-x", True), ("Hall4-", "xhall4-", False),
    ("Hall4-", "Hall4x", False),
])
def test_punctuation_at_a_token_edge_is_a_separator(tmp_path, gate, token, text, found):
    r = scan(gate, tmp_path, {"data/rows.csv": f"memo\n{text}\n"},
             tokens={**TOKENS, "place": [token]})
    assert (hit("c", "place", "data/rows.csv", token) in r.lines) is found


def test_file_and_directory_names_are_searched_and_printed_redacted(tmp_path, gate):
    files = {"Samplefamily/readme.md": "hello\n", "docs/notes-demo_booking_zztest.md": "hello\n"}
    r = scan(gate, tmp_path, files, rc=1)
    assert hit("c", "name-filename", f"[hmac:{lab('Samplefamily')}]", "Samplefamily") in r.lines
    assert hit("c", "identifier-filename", f"docs/notes-[hmac:{lab(ID)}].md", ID) in r.lines
    assert r.lines[-1] == "findings: 2 (accepted: 0)"


def test_places_are_searched_in_file_names_too(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/Tamarind-Lodge-notes.md": "x\n"}, rc=1)
    printed = f"docs/[hmac:{lab('Tamarind Lodge')}]-notes.md"
    assert hit("c", "place-filename", printed, "Tamarind Lodge") in r.lines


def test_a_file_name_cannot_forge_an_output_line(tmp_path, gate):
    r = scan(gate, tmp_path, {"evil\nc identifier forged.md\t\x1b[2K": f"{ID}\n"}, rc=1)
    assert r.lines == [hit("c", "identifier", "evil\\nc identifier forged.md\\t\\x1b[2K", ID),
                       "findings: 1 (accepted: 0)"]


def test_hex_lower_upper_and_embedded_identifiers_are_found(tmp_path, gate):
    h = ID.encode().hex()
    files = {"src/lower.py": f"blob = '{h}'\n", "src/upper.py": f"BLOB = 0x{h.upper()}\n",
             "src/inner.py": f"x = 'ff00{h}00ff'\n",
             "src/name.py": f"n = '{b'SAMPLEFAMILY'.hex()}'\n"}
    r = scan(gate, tmp_path, files, rc=1)
    for path in ("src/lower.py", "src/upper.py", "src/inner.py"):
        assert hit("c", "identifier-hex", path, ID) in r.lines
    assert hit("c", "name-hex", "src/name.py", "Samplefamily") in r.lines


def test_hex_of_a_three_byte_token_and_base64_of_a_five_byte_token_are_found(tmp_path, gate):
    tokens = {**TOKENS, "name": [*TOKENS["name"], "Kez", "Kezia"]}
    files = {"src/h.py": f"x = '00{b'Kez'.hex()}ff'\n"}
    for align in range(3):
        blob = base64.b64encode(b"<>?"[:align] + b"Kezia" + b" tail").decode()
        files[f"src/b{align}.js"] = f"b='{blob}'\n"
    r = scan(gate, tmp_path, files, rc=1, tokens=tokens)
    assert hit("c", "name-hex", "src/h.py", "Kez") in r.lines
    for align in range(3):
        assert hit("c", "name-base64", f"src/b{align}.js", "Kezia") in r.lines


def test_percent_encoded_tokens_are_found(tmp_path, gate):
    full = "".join(f"%{b:02X}" for b in ID.encode())
    files = {"docs/link.md": f"[mail](mailto:alex{PCT_AT}samplefamily.test)\n",
             "docs/full.md": f"q={full}\n", "docs/space.md": "x=hello%20samplefamily%20!\n",
             "docs/vendor.md": "q=Blorbo+%4Codge&x=1\n", "docs/place.md": "q=12%20Example%20Way\n"}
    r = scan(gate, tmp_path, files, rc=1)
    assert hit("c", "email-percent", "docs/link.md", ADDR) in r.lines
    assert hit("c", "identifier-percent", "docs/full.md", ID) in r.lines
    assert hit("c", "name-percent", "docs/space.md", "Samplefamily") in r.lines
    assert hit("c", "vendor-percent", "docs/vendor.md", "Blorbo Lodge") in r.lines
    assert hit("c", "place-percent", "docs/place.md", "12 Example Way") in r.lines


def test_unicode_escaped_and_html_entity_tokens_are_found(tmp_path, gate):
    files = {"src/esc.json": '{"n": "Zo\\u00eb Quillon", "m": "\\u0053amplefamily"}\n',
             "src/ent.html": "<p>Zo&#235; Quillon &#x53;amplefamily "
                             f"alex{ENT_AT}samplefamily.test</p>\n"}
    r = scan(gate, tmp_path, files, rc=1)
    assert hit("c", "name-unicode-escape", "src/esc.json", "Zoë Quillon") in r.lines
    assert hit("c", "name-unicode-escape", "src/esc.json", "Samplefamily") in r.lines
    assert hit("c", "name-html-entity", "src/ent.html", "Zoë Quillon") in r.lines
    assert hit("c", "email-html-entity", "src/ent.html", ADDR) in r.lines


@pytest.mark.parametrize("align", [0, 1, 2])
def test_base64_identifiers_are_found_at_each_byte_alignment(tmp_path, gate, align):
    blob = base64.b64encode(b"<>?"[:align] + ID.encode() + b" tail").decode()
    r = scan(gate, tmp_path, {"src/b.js": f"const b = '{blob}';\n"}, rc=1)
    assert hit("c", "identifier-base64", "src/b.js", ID) in r.lines


def test_base64_of_another_case_form_and_url_safe_base64_are_found(tmp_path, gate):
    url_safe = base64.urlsafe_b64encode("Zoë Quillon".encode() + b" tail").decode()
    assert "_" in url_safe
    files = {"src/b.js": f"x='{base64.b64encode(b'hi samplefamily!').decode()}'\n",
             "src/u.js": f"t='{url_safe}'\n"}
    r = scan(gate, tmp_path, files, rc=1)
    assert hit("c", "name-base64", "src/b.js", "Samplefamily") in r.lines
    assert hit("c", "name-base64", "src/u.js", "Zoë Quillon") in r.lines


def test_binaries_are_searched_as_utf8_and_utf16_runs(tmp_path, gate):
    blob = b"\x00\x01\x02" + ID.encode("utf-16-le") + b"\xff\xfe\x00" + b"pnr9k2m4x7" + b"\x00\x9c"
    files = {"assets/blob.bin": blob,
             "assets/u8.bin": b"\x00\x01" + "Zoë Quillon".encode() + b"\x00",
             "assets/glued8.bin": b"\x00\xc3\xa9Samplefamily\x00",
             "assets/glued16.bin": b"\x00\x01\x02" + "Samplefamily".encode("utf-16-be") + b"\x00",
             "assets/u16le.bin": b"\x01\x02\x00\x00" + "Zoë Quillon".encode("utf-16-le")
             + b"\x00\x00\x01",
             "assets/u16be.bin": b"\x01\x02\x00\x00" + "Zoë Quillon".encode("utf-16-be")
             + b"\x00\x00\x01"}
    r = scan(gate, tmp_path, files, rc=1)
    assert hit("c", "identifier", "assets/blob.bin", ID) in r.lines
    assert hit("c", "identifier", "assets/blob.bin", "pnr9k2m4x7") in r.lines
    for path in ("assets/u8.bin", "assets/u16le.bin", "assets/u16be.bin"):
        assert hit("c", "name", path, "Zoë Quillon") in r.lines
    assert hit("c", "name", "assets/glued8.bin", "Samplefamily") in r.lines
    assert hit("c", "name", "assets/glued16.bin", "Samplefamily") in r.lines


def test_bom_marked_utf16_and_utf32_and_non_utf8_text_are_decoded(tmp_path, gate):
    text = "by Zoë Quillon\n"
    files = {"docs/le.txt": b"\xff\xfe" + text.encode("utf-16-le"),
             "docs/be.txt": b"\xfe\xff" + text.encode("utf-16-be"),
             "docs/u32.txt": text.encode("utf-32"), "docs/cp.txt": text.encode("cp1252"),
             "docs/latin.dat": "café Samplefamily".encode("latin-1")}
    r = scan(gate, tmp_path, files, rc=1)
    for path in ("docs/le.txt", "docs/be.txt", "docs/u32.txt", "docs/cp.txt"):
        assert hit("c", "name", path, "Zoë Quillon") in r.lines
    assert hit("c", "name", "docs/latin.dat", "Samplefamily") in r.lines


def test_archive_members_are_scanned_as_outer_bang_member(tmp_path, gate):
    files = {
        "dist/bundle.zip": zip_bytes({"inner/notes.txt": f"id {ID}\n"}),
        "dist/pack.tar": tar_bytes({"a/b.md": f"{ID}\n"}),
        "dist/pack.tgz": gzip.compress(tar_bytes({"c.md": f"{ID}\n"})),
        "dist/rows.csv.gz": gzip.compress(b"x,2024-03-17\n"),
        "dist/nested.zip": zip_bytes({"deep.tar": tar_bytes({"d.md": f"{ID}\n"})}),
        "dist/book.xlsx": zip_bytes({"xl/sharedStrings.xml": "<t>Samplefamily</t>"}),
        "dist/named.zip": zip_bytes({"Samplefamily.txt": "hello"}),
    }
    r = scan(gate, tmp_path, files, rc=1)
    for path in ("dist/bundle.zip!inner/notes.txt", "dist/pack.tar!a/b.md", "dist/pack.tgz!c.md",
                 "dist/nested.zip!deep.tar!d.md"):
        assert hit("c", "identifier", path, ID) in r.lines
    assert hit("c", "date", "dist/rows.csv.gz!rows.csv", "2024-03-17") in r.lines
    assert hit("c", "name", "dist/book.xlsx!xl/sharedStrings.xml", "Samplefamily") in r.lines
    named = f"dist/named.zip![hmac:{lab('Samplefamily')}].txt"
    assert hit("c", "name-filename", named, "Samplefamily") in r.lines


def test_xz_lzma_bz2_and_mislabelled_archives_are_opened_by_magic_number(tmp_path, gate):
    body = f"id {ID}\n".encode()
    files = {"dist/n.txt.xz": lzma.compress(body),
             "dist/a.lzma": lzma.compress(body, format=lzma.FORMAT_ALONE),
             "dist/n.txt.bz2": bz2.compress(body),
             "dist/p.tar.xz": lzma.compress(tar_bytes({"c.md": ID})),
             "dist/mislabelled.zip": gzip.compress(tar_bytes({"c.md": ID})),
             "dist/blob.dat": bz2.compress(body), "dist/blob2.dat": lzma.compress(body)}
    r = scan(gate, tmp_path, files, rc=1)
    for path in ("dist/n.txt.xz!n.txt", "dist/a.lzma!a", "dist/n.txt.bz2!n.txt",
                 "dist/p.tar.xz!c.md", "dist/mislabelled.zip!c.md", "dist/blob.dat!blob.dat",
                 "dist/blob2.dat!blob2.dat"):
        assert hit("c", "identifier", path, ID) in r.lines


def test_data_after_the_last_xz_or_bz2_stream_is_a_finding(tmp_path, gate):
    tail = f"\nnotes by {ID.replace('_', '-')}\n".encode()
    files = {"dist/a.xz": lzma.compress(b"hello\n") + tail,
             "dist/a.bz2": bz2.compress(b"hello\n") + tail,
             "dist/noext": lzma.compress(b"hello\n") + tail,
             "dist/a.lzma": lzma.compress(b"hi\n", format=lzma.FORMAT_ALONE) + tail}
    r = scan(gate, tmp_path, files, rc=1)
    for path in files:
        assert hit("c", "archive-error", path) in r.lines


def test_concatenated_streams_and_nul_padding_decode_fully(tmp_path, gate):
    files = {"dist/two.xz": lzma.compress(b"first\n") + lzma.compress(f"{ID}\n".encode())
             + b"\0" * 8,
             "dist/two.bz2": bz2.compress(b"first\n") + bz2.compress(f"{ID}\n".encode())}
    r = scan(gate, tmp_path, files, rc=1)
    assert hit("c", "identifier", "dist/two.xz!two", ID) in r.lines
    assert hit("c", "identifier", "dist/two.bz2!two", ID) in r.lines
    assert not [line for line in r.lines if "archive-error" in line]


def test_zstd_is_decoded_where_python_has_it_and_is_otherwise_unsupported(tmp_path, gate):
    if pg.zstd is not None:
        files = {"dist/z.txt.zst": pg.zstd.compress(f"id {ID}\n".encode())}
        r = scan(gate, tmp_path, files, rc=1)
        assert hit("c", "identifier", "dist/z.txt.zst!z.txt", ID) in r.lines
    else:
        r = scan(gate, tmp_path, {"dist/z.txt.zst": b"\x28\xb5\x2f\xfd" + b"\x00" * 20}, rc=1)
        assert hit("c", "archive-unsupported", "dist/z.txt.zst") in r.lines


def _break_checksum(t: bytes) -> bytes:
    return t[:148] + b"0000000\0" + t[156:]


def _break_header(t: bytes) -> bytes:
    return t[:1024] + b"x" * 512 + t[1536:]


@pytest.mark.parametrize("path,body", [
    ("dist/broken.zip", b"PK\x03\x04garbage"),
    ("dist/broken.gz", b"\x1f\x8bnot gzip at all"),
    ("dist/broken.tar", _break_checksum(tar_bytes({"a.md": "hello"}))),
    ("dist/truncated.tar", tar_bytes({"a.md": "x" * 2000})[:1100]),
    ("dist/trailing.tar", tar_bytes({"a.md": "hello"}) + b"junk after the end"),
    ("dist/bad-header.tar", _break_header(tar_bytes({"a.md": "hello"}))),
    ("dist/sniffed.bin", b"\x1f\x8b\x08\x00broken"),
    ("dist/broken.xz", b"\xfd7zXZ\x00garbage"),
    ("dist/broken.bz2", b"BZh9garbage"),
    ("dist/cut.tgz", gzip.compress(tar_bytes({"a.md": "x" * 5000}))[:-40]),
])
def test_an_undecodable_archive_is_a_finding(tmp_path, gate, path, body):
    r = scan(gate, tmp_path, {path: body}, rc=1)
    assert hit("c", "archive-error", path) in r.lines


def test_a_file_named_like_an_archive_without_its_magic_number_is_probed_or_read(tmp_path, gate):
    files = {"var/lib/dpkg/alternatives/builtins.7.gz": f"/usr/share/man/man7/{ID}.7.gz\n",
             "dist/notes.tar": f"plain text about {ID}\n",
             "dist/prefixed.zip": b"junk before " + zip_bytes({"m.txt": ID})}
    r = scan(gate, tmp_path, files, rc=1)
    assert hit("c", "identifier", "var/lib/dpkg/alternatives/builtins.7.gz", ID) in r.lines
    assert hit("c", "identifier", "dist/notes.tar", ID) in r.lines
    assert hit("c", "identifier", "dist/prefixed.zip!m.txt", ID) in r.lines
    assert not [line for line in r.lines if "archive-error" in line]


def test_a_piece_boundary_does_not_invent_a_match(tmp_path, gate):
    filler = ("x" * 63 + " ") * (pg.CHUNK // 64 + 64)
    dotted_five = dotted(10, 0, 0, 5, 6)   # not an address: a fifth number follows its last dot
    at = pg.CHUNK - dotted_five.index(".6") - 1
    body = filler[:at - 1] + " " + dotted_five + " " + filler[at + len(dotted_five) + 1:]
    scan(gate, tmp_path, {"logs/big.log": body}, rc=0, parts="d")


def test_archives_nested_too_deep_are_a_finding(tmp_path, gate):
    body = zip_bytes({"leaf.txt": "x"})
    for _ in range(9):
        body = zip_bytes({"n.zip": body})
    r = scan(gate, tmp_path, {"dist/deep.zip": body}, rc=1)
    assert [line for line in r.lines if line.startswith("c archive-too-deep dist/deep.zip!n.zip!")]


def test_tar_member_metadata_zip_comments_and_the_gzip_file_name_are_searched(tmp_path, gate):
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w", format=tarfile.PAX_FORMAT) as t:
        info = tarfile.TarInfo("a.md")
        info.size, info.uname = 2, "Samplefamily"
        t.addfile(info, io.BytesIO(b"ok"))
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.comment = b"packed by Samplefamily"
        member = zipfile.ZipInfo("a.txt")
        member.comment = b"for pnr9k2m4x7"
        z.writestr(member, "ok")
    gbuf = io.BytesIO()
    with gzip.GzipFile(filename="export-Samplefamily.csv", fileobj=gbuf, mode="wb") as g:
        g.write(b"ok\n")
    r = scan(gate, tmp_path, {"dist/meta.tar": tbuf.getvalue(), "dist/c.zip": zbuf.getvalue(),
                              "dist/x.gz": gbuf.getvalue()}, rc=1)
    assert hit("c", "name", "dist/meta.tar!a.md", "Samplefamily") in r.lines
    assert hit("c", "name", "dist/c.zip", "Samplefamily") in r.lines
    assert hit("c", "identifier", "dist/c.zip!a.txt", "pnr9k2m4x7") in r.lines
    assert hit("c", "name", "dist/x.gz", "Samplefamily") in r.lines


def test_an_allow_listed_vendor_is_not_denied(tmp_path, gate):
    r = scan(gate, tmp_path, {"src/a.py": "# Quuxair and Blorbo Lodge\n"}, rc=1,
             allow_vendors=["QUUXAIR"])
    assert hit("c", "vendor", "src/a.py", "Blorbo Lodge") in r.lines
    assert not [line for line in r.lines if f"hmac:{lab('Quuxair')}" in line]


def test_an_allow_listed_place_is_not_denied(tmp_path, gate):
    r = scan(gate, tmp_path, {"src/a.py": "# 12 Example Way and Tamarind Lodge\n"}, rc=1,
             allow_vendors=["12 example  WAY"])
    assert hit("c", "place", "src/a.py", "Tamarind Lodge") in r.lines
    assert not [line for line in r.lines if f"hmac:{lab('12 Example Way')}" in line]


def test_a_path_scoped_allowance_applies_only_inside_its_glob(tmp_path, gate):
    files = {"docs/about.md": "by Samplefamily\n", "src/about.md": "by Samplefamily\n"}
    r = scan(gate, tmp_path, files, rc=1, allow_paths=[("name", "samplefamily", "docs/*")])
    assert hit("c", "name", "src/about.md", "Samplefamily") in r.lines
    assert not [line for line in r.lines if "docs/about.md" in line]


LITER = {**TOKENS, "name": [*TOKENS["name"], "Liter"]}
LITER_FILES = {"src/a.py": "literal = 1\nliterally = 2\n", "docs/b.md": "by Liter today\n",
               "docs/c.md": "Liter and literal\n"}


def test_a_kind_scoped_allowance_allows_only_that_kind(tmp_path, gate):
    r = scan(gate, tmp_path, LITER_FILES, rc=1, tokens=LITER,
             allow_paths=[("name", "liter", "*", "name-substring")])
    liter = sorted((f["path"], f["kind"], f["counts"]) for f in r.report["findings"]
                   if f["token_hmac"] == lab("Liter"))
    assert liter == [("docs/b.md", "name", {"": 1}), ("docs/c.md", "name", {"": 1})]
    assert r.report["summary"]["allowed"] == 3


def test_a_three_column_allowance_still_allows_every_kind(tmp_path, gate):
    r = scan(gate, tmp_path, LITER_FILES, tokens=LITER, allow_paths=[("name", "liter", "*")])
    assert not [f for f in r.report["findings"] if f["token_hmac"] == lab("Liter")]
    assert r.report["summary"]["allowed"] == 5


def test_a_kind_scoped_allowance_shows_only_that_kind_in_paths(tmp_path, gate):
    files = {"src/literal_notes.py": f"x = '{ID}'\n", "src/liter_notes.py": f"x = '{ID}'\n"}
    label = f"[hmac:{lab('Liter')}]"
    r = scan(gate, tmp_path, files, rc=1, tokens=LITER,
             allow_paths=[("name", "liter", "*", "name-substring")])
    # a content kind: names stay redacted
    assert hit("c", "identifier", f"src/{label}al_notes.py", ID) in r.lines
    r = scan(gate, tmp_path / "f", files, rc=1, tokens=LITER,
             allow_paths=[("name", "liter", "*", "name-substring-filename")])
    assert hit("c", "identifier", "src/literal_notes.py", ID) in r.lines
    assert hit("c", "identifier", f"src/{label}_notes.py", ID) in r.lines
    assert hit("c", "name-filename", f"src/{label}_notes.py", "Liter") in r.lines
    assert not [line for line in r.lines if "literal_notes" in line and "-filename" in line]


@pytest.mark.parametrize("line", ["name\tliter\t*\t", "name\tliter\t*\tname-substring\textra",
                                  "name\tliter\t*\tname-bogus", "name\tliter\t*\tvendor-substring"])
def test_a_bad_kind_column_is_an_error(tmp_path, gate, line):
    d = write_deny(tmp_path, tokens=LITER)
    (d / "allow-paths.tsv").write_text(line + "\n")
    r = gate("scan", "--tree", tree(tmp_path, {"a.md": "x\n"}), "--denylist", d, rc=2)
    assert "allow-paths.tsv, line 1" in r.err


def test_an_accepted_hit_is_reported_as_accepted_not_as_a_finding(tmp_path, gate):
    r = scan(gate, tmp_path, {"notes.md": f"id {ID}\n"}, rc=0, accepted=[(lab(ID), "notes.md")])
    assert r.lines == ["accepted " + hit("c", "identifier", "notes.md", ID),
                       "findings: 0 (accepted: 1)"]
    assert r.report["accepted"] == [{"part": "c", "kind": "identifier", "path": "notes.md",
                                     "token_hmac": lab(ID), "count": 1, "line": 1,
                                     "counts": {"": 1}}]


def test_an_accepted_hit_elsewhere_still_counts(tmp_path, gate):
    files = {"notes.md": f"{ID}\n", "other.md": f"{ID}\n"}
    r = scan(gate, tmp_path, files, rc=1, accepted=[(lab(ID), "notes.md")])
    assert r.lines[-1] == "findings: 1 (accepted: 1)"


def test_short_tokens_are_ignored_with_one_counted_warning(tmp_path, gate):
    tokens = {**TOKENS, "name": [*TOKENS["name"], "Al", "x"]}
    r = scan(gate, tmp_path, {"a.md": "Al x\n"}, rc=0, tokens=tokens)
    assert "warning: 2 denylist tokens shorter than 3 characters are ignored" in r.err.splitlines()


def test_a_fifo_is_a_finding_not_a_hang(tmp_path, gate):
    t = tree(tmp_path, {"a.md": "ok\n"})
    os.mkfifo(t / "pipe")
    r = gate("scan", "--tree", t, "--denylist", write_deny(tmp_path), rc=1)
    assert hit("c", "special-file", "pipe") in r.lines


def test_a_symlink_target_is_searched(tmp_path, gate):
    t = tree(tmp_path, {"a.md": "ok\n"})
    os.symlink(f"../{ID}/x", t / "link")
    r = gate("scan", "--tree", t, "--denylist", write_deny(tmp_path), rc=1)
    assert hit("c", "identifier", "link", ID) in r.lines


# --- glued, camel-cased and escape-prefixed names ------------------------------------------------

GLUED_TOKENS = {**TOKENS, "name": [*TOKENS["name"], "Alexandria", "Quill", "Bram"],
                "vendor": [*TOKENS["vendor"], "Quillworks"],
                "email": [*TOKENS["email"], f"quillfeather{AT}blorbomail.test"],
                "host": [*TOKENS["host"], "vault.quillworks.test"]}
QUILLFEATHER = f"quillfeather{AT}blorbomail.test"


@pytest.mark.parametrize("text,kind,token", [
    ("const alexandriaShare = 1;", "name", "Alexandria"),
    ("class AlexandriaQuill:", "name", "Alexandria Quill"),
    ("HTMLQuillworksExport", "vendor", "Quillworks"),
    ("BlorboLodgeStay", "vendor", "Blorbo Lodge"),
    ('{"note": "Owner:\\nAlexandria"}', "name", "Alexandria"),
    ('{"note": "Owner:\\talexandria quill"}', "name", "Alexandria Quill"),
    ("alexandriaquill", "name-substring", "Alexandria Quill"),
    ("the Quills", "name-substring", "Quill"),
    ("thequillworks", "name-substring", "Quill"),
    ("signed quillfeather2030", "email-substring", QUILLFEATHER),
    ("relay-blorbomailtest01", "email-substring", QUILLFEATHER),
    ("vaultquillworkstest", "host-substring", "vault.quillworks.test"),
])
def test_glued_camel_cased_and_escape_prefixed_names_are_found(tmp_path, gate, text, kind, token):
    r = scan(gate, tmp_path, {"src/a.txt": f"{text}\n"}, rc=1, tokens=GLUED_TOKENS)
    assert hit("c", kind, "src/a.txt", token) in r.lines


LONG_RUN = "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6kL9xKezQ2wE5rT8yU1iO4pA7sD0fG3"


@pytest.mark.parametrize("text,found", [
    (f"data:image/png;base64,{LONG_RUN}==", False), ("by x Kez y", True), ("getKezValue()", True),
])
def test_a_long_alphanumeric_run_gets_no_camel_case_boundaries(tmp_path, gate, text, found):
    assert len(LONG_RUN) > pg.MAX_CAMEL_RUN and b"Kez".hex() not in LONG_RUN.lower()
    tokens = {**TOKENS, "name": [*TOKENS["name"], "Kez"]}
    r = scan(gate, tmp_path, {"src/a.txt": f"{text}\n"}, tokens=tokens)
    assert (hit("c", "name", "src/a.txt", "Kez") in r.lines) is found


@pytest.mark.parametrize("text", ["getAlexandriaQuill()", "AlexandriaQuillNotes"])
def test_camel_case_names_in_ordinary_identifiers_are_still_found(tmp_path, gate, text):
    r = scan(gate, tmp_path, {"src/a.txt": f"{text}\n"}, rc=1)
    assert hit("c", "name", "src/a.txt", "Alexandria Quill") in r.lines


@pytest.mark.parametrize("text", ["the brambles", "Brambles", "BRAMBLES"])
def test_a_four_letter_name_inside_a_longer_word_is_not_found(tmp_path, gate, text):
    r = scan(gate, tmp_path, {"src/a.txt": f"{text}\n"}, rc=0, tokens=GLUED_TOKENS)
    assert r.lines == ["findings: 0 (accepted: 0)"]


@pytest.mark.parametrize("name,kind,token", [
    ("AlexandriaQuillNotes.md", "name-filename", "Alexandria Quill"),
    ("thequills.md", "name-substring-filename", "Quill"),
])
def test_file_names_get_the_camel_case_view_and_substring_matching(tmp_path, gate, name, kind,
                                                                    token):
    r = scan(gate, tmp_path, {f"docs/{name}": "x\n"}, rc=1, tokens=GLUED_TOKENS)
    assert [line for line in r.lines
            if line.startswith(f"c {kind} docs/") and line.endswith(f" hmac:{lab(token)}")]


# --- dates in file names -------------------------------------------------------------------------

DATE_TOKENS = {**TOKENS, "date": [*TOKENS["date"], "2030-03-17"]}


@pytest.mark.parametrize("name,kind,token", [
    ("export_2030-03-17.json", "date-filename", "2030-03-17"),
    ("report-2030-03-17T0815.md", "date-filename", "2030-03-17"),
    ("20300317.csv", "date-filename", "2030-03-17"),
    ("itinerary-2024-03-17.txt", "date-filename", "2024-03-17"),
])
def test_date_tokens_are_found_in_file_names(tmp_path, gate, name, kind, token):
    r = scan(gate, tmp_path, {f"exports/{name}": "x\n"}, rc=1, tokens=DATE_TOKENS)
    assert [line for line in r.lines
            if line.startswith(f"c {kind} exports/") and line.endswith(f" hmac:{lab(token)}")]
    assert name not in r.out and "20300317" not in r.out


def test_digits_glued_to_letters_in_a_file_name_are_not_a_date(tmp_path, gate):
    files = {"exports/v20300317x.csv": "x\n", "exports/V20300317X.csv": "x\n"}
    r = scan(gate, tmp_path, files, rc=0, tokens=DATE_TOKENS)
    assert r.lines == ["findings: 0 (accepted: 0)"]


# --- counts and line numbers ---------------------------------------------------------------------

def test_a_finding_carries_its_count_and_the_line_of_its_first_match(tmp_path, gate):
    files = {"docs/a.md": "intro\nby Samplefamily\nnothing\nSamplefamily and SAMPLEFAMILY again\n",
             "notes.md": f"id {ID}\n",
             "docs/le.txt": b"\xff\xfe" + "x\r\ny\r\nby Zoë Quillon\r\n".encode("utf-16-le")}
    r = scan(gate, tmp_path, files, rc=1)
    assert f"c name docs/a.md:2 hmac:{lab('Samplefamily')} ×3" in r.raw_lines
    assert f"c identifier notes.md:1 hmac:{lab(ID)}" in r.raw_lines
    assert f"c name docs/le.txt:3 hmac:{lab('Zoë Quillon')}" in r.raw_lines
    found = {(f["path"], f["kind"]): (f["count"], f["line"]) for f in r.report["findings"]}
    assert found[("docs/a.md", "name")] == (3, 2) and found[("notes.md", "identifier")] == (1, 1)


def test_binaries_and_archive_members_have_no_line(tmp_path, gate):
    files = {"assets/blob.bin": b"\x00\x01" + f"x\n{ID}\n{ID}".encode() + b"\x00",
             "dist/a.zip": zip_bytes({"m.txt": f"x\n{ID}\n"})}
    r = scan(gate, tmp_path, files, rc=1)
    found = {f["path"]: (f["count"], f["line"]) for f in r.report["findings"]}
    assert found == {"assets/blob.bin": (2, None), "dist/a.zip!m.txt": (1, None)}
    assert hit("c", "identifier", "assets/blob.bin", ID) + " ×2" in r.raw_lines


def _dotted_body(blocks: int) -> bytearray:
    return bytearray((b"." * 8191 + b"\n") * blocks)


def test_counts_are_exact_across_a_piece_boundary(tmp_path, gate):
    """One token before the overlap, one wholly inside it (both pieces read it), one across the cut
    and one after it; a second token only in the second piece checks that line numbers continue
    across pieces."""
    deny = write_deny(tmp_path)
    overlap = pg.Denylist.load(deny).overlap
    token = f".{ID}.".encode()
    body = _dotted_body((pg.CHUNK + (512 << 10)) // 8192)

    def put(at: int, data: bytes = token) -> None:
        body[at:at + len(data)] = data
    put(pg.CHUNK - 8)
    cut = pg._cut(bytes(body), pg.CHUNK, 1)
    keep = pg._cut(bytes(body), cut - overlap, 1)
    assert pg.CHUNK - 8 < cut < pg.CHUNK - 8 + len(token) and keep + 4 + len(token) <= cut
    put(9000)
    put(keep + 4)
    put(pg.CHUNK + 20000)
    put(pg.CHUNK + 30000, b".pnr9k2m4x7.")
    data = bytes(body)
    assert (pg._cut(data, pg.CHUNK, 1), pg._cut(data, cut - overlap, 1)) == (cut, keep)
    r = gate("scan", "--tree", tree(tmp_path, {"logs/big.log": data}), "--denylist", deny, rc=1)
    found = {f["token_hmac"]: (f["count"], f["line"]) for f in r.report["findings"]}
    assert found[lab(ID)] == (4, 2)
    assert found[lab("pnr9k2m4x7")] == (1, data[:pg.CHUNK + 30000].count(b"\n") + 1)


def test_plain_and_camel_split_occurrences_are_counted_once_each(tmp_path, gate):
    tokens = {**TOKENS, "name": [*TOKENS["name"], "Quillfeather"]}
    body = "QuillFeather QuillFeather fooQuillfeather barQuillfeather\n"
    r = scan(gate, tmp_path, {"src/a.txt": body}, rc=1, tokens=tokens)
    assert f"c name src/a.txt:1 hmac:{lab('Quillfeather')} ×4" in r.raw_lines
    assert [f["counts"] for f in r.report["findings"]] == [{"": 4}]


def test_each_spelling_is_counted_in_its_own_mode(tmp_path, gate):
    body = f"{ID}\n{ID.encode().hex()}\nAlexandria Quill alexandriaquill\nAlexandria%20Quill\n"
    r = scan(gate, tmp_path, {"src/a.txt": body}, rc=1)
    found = {f["token_hmac"]: (f["kind"], f["counts"], f["count"]) for f in r.report["findings"]}
    assert found[lab(ID)] == ("identifier", {"": 1, "hex": 1}, 2)
    assert found[lab("Alexandria Quill")] == (
        "name", {"": 1, "substring": 1, "percent": 1}, 3)


def test_per_mode_counts_are_exact_across_a_piece_boundary(tmp_path, gate):
    """Plain hits before the overlap, inside it and across the cut, and hex hits inside the overlap
    and after the cut: each mode is counted once per occurrence, whichever piece reads it."""
    deny = write_deny(tmp_path)
    overlap = pg.Denylist.load(deny).overlap
    plain, hexed = f".{ID}.".encode(), f".{ID.encode().hex()}.".encode()
    body = _dotted_body((pg.CHUNK + (512 << 10)) // 8192)

    def put(at: int, data: bytes) -> None:
        body[at:at + len(data)] = data
    put(pg.CHUNK - 8, plain)
    cut = pg._cut(bytes(body), pg.CHUNK, 1)
    keep = pg._cut(bytes(body), cut - overlap, 1)
    assert keep + 50 + len(hexed) <= cut
    put(9000, plain)
    put(keep + 4, plain)
    put(keep + 50, hexed)
    put(pg.CHUNK + 20000, hexed)
    data = bytes(body)
    assert (pg._cut(data, pg.CHUNK, 1), pg._cut(data, cut - overlap, 1)) == (cut, keep)
    r = gate("scan", "--tree", tree(tmp_path, {"logs/big.log": data}), "--denylist", deny, rc=1)
    assert [(f["kind"], f["counts"], f["count"], f["line"]) for f in r.report["findings"]] == [
        ("identifier", {"": 3, "hex": 2}, 5, 2)]


def test_files_that_print_alike_stay_apart_and_an_ambiguous_printed_path_accepts_neither(
        tmp_path, gate):
    printed = f"docs/[hmac:{lab('Alexandria Quill')}].md"
    both = {"docs/alexandriaquill.md": f"{ID}\n", "docs/AlexandriaQuill.md": f"{ID}\n"}
    r = scan(gate, tmp_path, both, rc=1)
    assert r.lines.count(hit("c", "identifier", printed, ID)) == 2
    r = scan(gate, tmp_path / "two", both, rc=1, accepted=[(lab(ID), printed)])
    assert r.lines.count(hit("c", "identifier", printed, ID)) == 2
    assert not [line for line in r.lines if line.startswith("accepted")]
    r = scan(gate, tmp_path / "one", {"docs/AlexandriaQuill.md": f"{ID}\n"}, rc=1,
             accepted=[(lab(ID), printed)])
    assert "accepted " + hit("c", "identifier", printed, ID) in r.lines
    assert hit("c", "identifier", printed, ID) not in r.lines


def test_the_json_summary_counts_occurrences_tokens_allowances_and_files(tmp_path, gate):
    d = write_deny(tmp_path, allow_vendors=["quuxair"],
                   allow_paths=[("name", "samplefamily", "docs/*")])
    USED.add("Quillfeather")
    (d / "manual.tsv").write_text("name\tQuillfeather\nname\tSAMPLEFAMILY\n")
    files = {"docs/a.md": "Samplefamily, Samplefamily\n", "src/b.md": f"{ID} {ID}\nQuillfeather\n"}
    r = gate("scan", "--tree", tree(tmp_path, files), "--denylist", d, rc=1)
    summary = r.report["summary"]
    keys = ("findings", "accepted", "occurrences", "allowed", "denylist_files")
    assert {k: summary[k] for k in keys} == {
        "findings": 2, "accepted": 0, "occurrences": 3, "allowed": 2, "denylist_files": 2}
    expected = {cls: len({fold(t) for t in tokens}) for cls, tokens in TOKENS.items()}
    expected["name"] += 1
    expected["vendor"] -= 1
    assert summary["tokens_by_class"] == expected
    assert list(summary["tokens_by_class"]) == list(pg.CLASSES)


# --- path display: only what matched is redacted -------------------------------------------------

def test_a_path_shows_only_the_matched_runs_redacted(tmp_path, gate):
    r = scan(gate, tmp_path, {"tests/test_alexandria_quill_rules.py": f"x = '{ID}'\n"}, rc=1)
    shown = f"tests/test_[hmac:{lab('Alexandria Quill')}]_rules.py"
    assert hit("c", "name-filename", shown, "Alexandria Quill") in r.lines
    assert hit("c", "identifier", shown, ID) in r.lines


def test_the_label_in_a_path_is_the_one_the_label_command_prints(tmp_path, gate):
    d = write_deny(tmp_path)
    r = gate("scan", "--tree", tree(tmp_path, {"docs/notes-Alexandria-Quill.md": "x\n"}),
             "--denylist", d, rc=1)
    printed = gate("label", "--denylist", d, stdin="Alexandria Quill\n", rc=0).lines
    assert r.lines[0] == f"c name-filename docs/notes-[hmac:{printed[0]}].md hmac:{printed[0]}"


def test_a_token_allowed_for_a_path_is_not_redacted_in_that_path(tmp_path, gate):
    both = f"docs/samplefamily-[hmac:{lab(ID)}].md"
    files = {"docs/samplefamily-notes.md": f"{ID}\n", "src/samplefamily-notes.md": f"{ID}\n",
             f"docs/samplefamily-{ID}.md": "x\n"}
    r = scan(gate, tmp_path, files, rc=1, allow_paths=[("name", "samplefamily", "docs/*")],
             raw_ok=("docs/samplefamily-notes.md", both))
    assert hit("c", "identifier", "docs/samplefamily-notes.md", ID) in r.lines
    assert hit("c", "identifier", f"src/[hmac:{lab('Samplefamily')}]-notes.md", ID) in r.lines
    assert hit("c", "identifier-filename", both, ID) in r.lines


def test_overlapping_matches_in_a_component_redact_the_whole_component(tmp_path, gate):
    tokens = {**TOKENS, "name": [*TOKENS["name"], "Quillfeather", "Featherstone"]}
    r = scan(gate, tmp_path, {"docs/quillfeatherstone.md": "x\n"}, rc=1, tokens=tokens)
    pattern = rf"c name-substring-filename docs/{REDACTED} hmac:{lab('Quillfeather')}"
    assert any(re.fullmatch(pattern, line) for line in r.lines)


@pytest.mark.parametrize("files,where", [
    ({"docs/Statement%20Alexandria%20Quill.pdf": "x\n"}, "docs/"),
    ({"docs/&#65;lexandria Quill.md": "x\n"}, "docs/"),
    ({"docs/\\u0041lexandria quill.md": "x\n"}, "docs/"),
    ({"dist/a.zip": zip_bytes({"Alexandria%20Quill.txt": "x\n"})}, "dist/a.zip!"),
])
def test_a_token_found_only_through_a_decoded_view_redacts_the_whole_component(
        tmp_path, gate, files, where):
    tokens = {**TOKENS, "name": [*TOKENS["name"], "Quill"]}
    r = scan(gate, tmp_path, files, rc=1, tokens=tokens)
    shown = [line for line in r.lines if line.startswith("c ")]
    pattern = rf"c \S+ {re.escape(where)}{REDACTED} hmac:[0-9a-f]{{12}}"
    assert shown and all(re.fullmatch(pattern, line) for line in shown), shown
    assert all(re.fullmatch(rf"{re.escape(where)}{REDACTED}", f["path"])
               for f in r.report["findings"])


def test_what_cannot_be_redacted_in_part_is_redacted_whole(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/Zoë Quillon.md": "x\n", "Alexandria/Quill.md": f"{ID}\n"}, rc=1)
    zoe = rf"c name-filename docs/{REDACTED} hmac:{lab('Zoë Quillon')}"
    assert any(re.fullmatch(zoe, line) for line in r.lines)
    assert any(re.fullmatch(rf"c identifier {REDACTED} hmac:{lab(ID)}", line) for line in r.lines)


# --- combos --------------------------------------------------------------------------------------

COMBO_A = TOKENS["combo"][0]          # Robin Sample + QQ4X7P
COMBO_B = TOKENS["combo"][1]          # Casa Zorblax + Alex Sample + ZZPNR1
BOTH = "Robin Sample booked QQ4X7P\n"


def test_both_parts_in_one_file_are_one_finding(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/a.md": f"intro\n{BOTH}again QQ4X7P and Robin Sample\n"}, rc=1)
    assert r.lines == [hit("c", "combo", "docs/a.md", COMBO_A), "findings: 1 (accepted: 0)"]
    assert r.raw_lines[0] == f"c combo docs/a.md:2 hmac:{lab(COMBO_A)}"
    assert r.report["findings"][0]["count"] == 1 and r.report["findings"][0]["counts"] == {"": 1}


def test_the_line_of_a_combo_is_that_of_its_first_part(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/a.md": "x\nbooked QQ4X7P\nfor Robin Sample\n"}, rc=1)
    assert r.raw_lines[0] == f"c combo docs/a.md:3 hmac:{lab(COMBO_A)}"


def test_parts_in_two_files_are_no_finding(tmp_path, gate):
    files = {"docs/a.md": "for Robin Sample\n", "docs/b.md": "ref QQ4X7P\n",
             "docs/c.md": "Casa Zorblax and Alex Sample\n", "docs/d.md": "ZZPNR1\n"}
    r = scan(gate, tmp_path, files, rc=0)
    assert r.lines == ["findings: 0 (accepted: 0)"]


@pytest.mark.parametrize("body", ["for Robin Sample\n", "ref QQ4X7P\n", "Casa Zorblax ZZPNR1\n",
                                  "Alex Sample ZZPNR1\n", "Robin Sample\n" * 50])
def test_one_part_alone_is_no_finding(tmp_path, gate, body):
    r = scan(gate, tmp_path, {"docs/a.md": body}, rc=0)
    assert r.lines == ["findings: 0 (accepted: 0)"]


def test_a_three_part_combo_needs_all_three_parts(tmp_path, gate):
    files = {"docs/two.md": "Casa Zorblax, Alex Sample\n",
             "docs/three.md": "Casa Zorblax, Alex Sample, ref ZZPNR1\n"}
    r = scan(gate, tmp_path, files, rc=1)
    assert r.lines == [hit("c", "combo", "docs/three.md", COMBO_B), "findings: 1 (accepted: 0)"]


def test_two_combos_in_one_file_are_two_findings(tmp_path, gate):
    body = BOTH + "Casa Zorblax Alex Sample ZZPNR1\n"
    r = scan(gate, tmp_path, {"docs/a.md": body}, rc=1)
    assert sorted(r.lines[:-1]) == sorted([hit("c", "combo", "docs/a.md", COMBO_A),
                                           hit("c", "combo", "docs/a.md", COMBO_B)])


@pytest.mark.parametrize("path,body", [
    ("src/a.py", "# Robin Sample, QQ4X7P\n"), ("docs/a.xyz", "QQ4X7P Robin Sample\n"),
    ("README", BOTH), ("data/a.json", '{"n": "Robin Sample", "r": "qq4x7p"}\n'),
    ("docs/a.md", "robin-sample_qq4x7p\n"), ("docs/b.md", "ROBIN\nSAMPLE\n\nqq4x7p\n"),
    ("src/camel.ts", "const RobinSample_QQ4X7P = 1;\n"),
    ("assets/a.bin", b"\x00\x01Robin Sample\x00\x02 QQ4X7P\x00"),
    ("assets/u16.bin",
     b"\x01\x02\x00\x00" + "Robin Sample QQ4X7P".encode("utf-16-le") + b"\x00\x01"),
    ("docs/bom.txt", b"\xff\xfe" + BOTH.encode("utf-16-le")),
])
def test_a_combo_is_searched_in_every_file_under_the_normal_boundary_rule(
        tmp_path, gate, path, body):
    r = scan(gate, tmp_path, {path: body}, rc=1)
    assert hit("c", "combo", path, COMBO_A) in r.lines


@pytest.mark.parametrize("body", ["xrobin sample qq4x7p\n", "robin samples qq4x7p\n",
                                  "robin sample qq4x7pp\n", "robin sample 1qq4x7p\n"])
def test_a_part_inside_a_longer_run_does_not_count(tmp_path, gate, body):
    scan(gate, tmp_path, {"docs/a.md": body}, rc=0)


def test_a_combo_is_not_searched_for_in_encoded_forms(tmp_path, gate):
    p1, p2 = COMBO_A.split(SEP)
    files = {"docs/pct.md": "Robin%20Sample QQ4X7P\n", "docs/pct2.md": "Robin Sample %51Q4X7P\n",
             "docs/hex.md": f"{p1.encode().hex()} {p2.encode().hex()}\n",
             "docs/b64.md": f"{base64.b64encode(p1.encode()).decode()} "
                            f"{base64.b64encode(p2.encode()).decode()}\n",
             "docs/esc.md": "\\u0052obin Sample QQ4X7P\n",
             "docs/ent.md": "&#82;obin Sample QQ4X7P\n"}
    r = scan(gate, tmp_path, files, rc=0)
    assert r.lines == ["findings: 0 (accepted: 0)"]


def test_a_combo_is_not_a_file_name_finding(tmp_path, gate):
    files = {"docs/Robin-Sample-QQ4X7P.md": "nothing here\n",
             "docs/Casa Zorblax/Alex Sample.md": "x\n"}
    r = scan(gate, tmp_path, files, rc=0)
    assert r.lines == ["findings: 0 (accepted: 0)"]


def test_a_part_in_a_printed_path_is_redacted_like_a_token(tmp_path, gate):
    files = {"docs/QQ4X7P-notes.md": f"{ID}\n", "docs/Robin Sample/trip.md": f"{ID}\n"}
    r = scan(gate, tmp_path, files, rc=1)
    assert hit("c", "identifier", f"docs/[hmac:{lab('QQ4X7P')}]-notes.md", ID) in r.lines
    assert hit("c", "identifier", f"docs/[hmac:{lab('Robin Sample')}]/trip.md", ID) in r.lines
    assert not [line for line in r.lines if "filename" in line]


def test_a_combo_in_a_file_whose_name_holds_a_part_prints_the_part_redacted(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/QQ4X7P.md": BOTH}, rc=1)
    printed = f"docs/[hmac:{lab('QQ4X7P')}].md"
    assert r.lines == [hit("c", "combo", printed, COMBO_A), "findings: 1 (accepted: 0)"]


def test_combo_parts_are_per_member_not_per_archive(tmp_path, gate):
    files = {"dist/split.zip": zip_bytes({"one.txt": "Robin Sample\n", "two.txt": "QQ4X7P\n"}),
             "dist/whole.zip": zip_bytes({"one.txt": BOTH}),
             "dist/whole.tgz": gzip.compress(tar_bytes({"deep/a.md": BOTH}))}
    r = scan(gate, tmp_path, files, rc=1)
    assert sorted(r.lines[:-1]) == sorted([
        hit("c", "combo", "dist/whole.zip!one.txt", COMBO_A),
        hit("c", "combo", "dist/whole.tgz!deep/a.md", COMBO_A)])


def test_a_combo_is_found_across_a_piece_boundary_with_the_line_of_its_first_part(tmp_path, gate):
    deny = write_deny(tmp_path)
    body = _dotted_body((pg.CHUNK + (512 << 10)) // 8192)
    first, second = b" Robin Sample ", b" QQ4X7P "
    body[3 * 8192 + 100:3 * 8192 + 100 + len(first)] = first
    body[pg.CHUNK + 20000:pg.CHUNK + 20000 + len(second)] = second
    data = bytes(body)
    r = gate("scan", "--tree", tree(tmp_path, {"logs/big.log": data}), "--denylist", deny, rc=1)
    assert r.raw_lines[0] == f"c combo logs/big.log:4 hmac:{lab(COMBO_A)}"
    assert r.lines[-1] == "findings: 1 (accepted: 0)"
    alone = bytes(_dotted_body(8)) + second
    r = gate("scan", "--tree", tree(tmp_path, {"logs/small.log": alone}, "t2"),
             "--denylist", deny, rc=0)
    assert r.lines == ["findings: 0 (accepted: 0)"]


def test_a_combo_is_only_searched_by_part_c(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/a.md": BOTH}, rc=0, parts="d")
    assert r.lines == ["findings: 0 (accepted: 0)"]


def test_a_path_scoped_allowance_applies_to_a_combo(tmp_path, gate):
    files = {"docs/a.md": BOTH, "src/a.md": BOTH}
    r = scan(gate, tmp_path, files, rc=1, allow_paths=[("combo", COMBO_A.lower(), "docs/*")])
    assert r.lines == [hit("c", "combo", "src/a.md", COMBO_A), "findings: 1 (accepted: 0)"]
    assert r.report["summary"]["allowed"] == 1


def test_an_accepted_combo_is_reported_as_accepted(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/a.md": BOTH}, rc=0, accepted=[(lab(COMBO_A), "docs/a.md")])
    assert r.lines == ["accepted " + hit("c", "combo", "docs/a.md", COMBO_A),
                       "findings: 0 (accepted: 1)"]


def test_unusable_combos_are_ignored_and_counted_with_the_short_tokens(tmp_path, gate):
    bad = [f"Ab{SEP}QQ4X7P", "OnlyOnePart", f"Robin Sample{SEP}", f"Robin Sample{SEP}ROBIN  sample",
           f"{SEP}QQ4X7P"]
    tokens = {"name": ["Samplefamily"], "combo": bad}
    r = scan(gate, tmp_path, {"docs/a.md": f"{BOTH}Ab QQ4X7P OnlyOnePart\n"}, rc=0, tokens=tokens)
    assert "warning: 5 denylist tokens shorter than 3 characters are ignored" in r.err.splitlines()
    assert r.report["summary"]["tokens_by_class"]["combo"] == 0


@pytest.mark.parametrize("padded", [f"Zorblax{SEP} ab", f"Zorblax {SEP}  ab ", f"Zorblax{SEP}ab"])
def test_whitespace_beside_the_separator_does_not_count_toward_a_part(tmp_path, gate, padded):
    tokens = {"name": ["Samplefamily"], "combo": [padded]}
    r = scan(gate, tmp_path, {"docs/a.md": "Zorblax and ab\n"}, rc=0, tokens=tokens)
    assert "warning: 1 denylist tokens shorter than 3 characters are ignored" in r.err.splitlines()


def test_a_padded_and_an_unpadded_spelling_of_a_combo_are_one_token(tmp_path, gate):
    plain, padded = f"Zorblax{SEP}Quuxtrip", f"Zorblax {SEP}  Quuxtrip "
    assert lab(plain) == lab(padded)
    d = write_deny(tmp_path, tokens={"name": ["Samplefamily"], "combo": [padded]})
    lines = gate("label", "--denylist", d, stdin=f"{plain}\n{padded}\nZorblax\\x1f Quuxtrip\n",
                 rc=0).lines
    assert lines == [lab(plain)] * 3
    t = tree(tmp_path, {"docs/a.md": "Quuxtrip, Zorblax\n"})
    r = gate("scan", "--tree", t, "--denylist", d, rc=1)
    assert r.lines == [hit("c", "combo", "docs/a.md", plain), "findings: 1 (accepted: 0)"]


def test_an_unusable_combo_given_straight_to_the_denylist_is_ignored(tmp_path):
    key = KEY
    deny = pg.Denylist([("combo", f"Zorblax{SEP}ab"), ("combo", "Zorblax"),
                        ("name", "Samplefamily")], key)
    assert deny.combos == {} and deny.short == 2


def test_a_combo_with_fewer_than_two_parts_is_never_reported(tmp_path):
    deny = pg.Denylist([("name", "Samplefamily")], KEY)
    deny.combos[""] = ()
    deny.combos["one"] = ("zorblax",)
    sc = pg.Scanner(deny, {"c"})
    sc.seen_parts["docs/a.md"] = {"zorblax": 1}
    assert sc.results() == ([], [])


def test_a_combo_of_four_parts_is_an_error_naming_no_part(tmp_path, gate):
    four = f"Robin Sample{SEP}QQ4X7P{SEP}ZZPNR1{SEP}Casa Zorblax"
    tokens = {"name": ["Samplefamily"], "combo": [four]}
    r = scan(gate, tmp_path, {"docs/a.md": "x\n"}, rc=2, tokens=tokens)
    assert r.out == "" and "denylist file #1, line 4: a combo has two or three parts" in r.err


def test_combos_count_as_tokens_by_class(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/a.md": "x\n"}, rc=0)
    assert r.report["summary"]["tokens_by_class"]["combo"] == 2
    assert r.report["summary"]["tokens_by_class"]["place"] == 2


# --- (d) generic ---------------------------------------------------------------------------------

def test_generic_patterns_are_findings(tmp_path, gate):
    files = {
        "docs/a.md": f"mail {OTHER} or ok{AT}example.com, x{AT}EXAMPLE.NET, y{AT}anthropic.com\n",
        "src/net.py": "".join(f"{name} = '{value}'\n" for name, value in [
            ("A", dotted(10, 1, 2, 3)), ("B", dotted(172, 20, 0, 5)),
            ("C", dotted(192, 168, 1, 10)), ("D", dotted(8, 8, 8, 8)),
            ("E", dotted(999, 1, 1, 1)), ("F", f"HOST_{dotted(192, 168, 4, 4)}"),
            ("G", dotted(1, 10, 0, 0, 1)), ("H", dotted(10, 0, 0, 1, 4))]),
        "src/home.sh": f"cd {HOME}/alex/projects\n",
        "src/mac.sh": f"open {USERS}/alex/Desktop\n",
        "app/package-lock.json": json.dumps(
            {"a": {"resolved": "https://npm.samplecorp.test/x.tgz"}}),
        "uv.lock": 'a = { registry = "https://pypi.org/simple" }\n'
                   'b = { registry = "https://mirror.samplecorp.test:8443/simple" }\n',
        "data/notes.json": json.dumps({"resolved": "https://npm.samplecorp.test/x.tgz"}),
    }
    r = scan(gate, tmp_path, files, rc=1, parts="d")
    assert hit("d", "email", "docs/a.md", OTHER) in r.lines
    for ip in (dotted(10, 1, 2, 3), dotted(172, 20, 0, 5), dotted(192, 168, 1, 10),
               dotted(192, 168, 4, 4)):
        assert hit("d", "private-ip", "src/net.py", ip) in r.lines
    assert hit("d", "home-path", "src/home.sh", f"{HOME}/alex") in r.lines
    assert hit("d", "home-path", "src/mac.sh", f"{USERS}/alex") in r.lines
    assert hit("d", "lock-host", "uv.lock", "mirror.samplecorp.test") in r.lines
    assert len(r.lines) - 1 == 8, r.lines


@pytest.mark.parametrize("text,match", [
    (f"cd {HOME}/alex", f"{HOME}/alex"), (f"cd {HOME}/alex/x", f"{HOME}/alex"),
    (f"open {USERS}/alex", f"{USERS.lower()}/alex"),
    (f"open {USERS.lower()}/Alex/Desktop", f"{USERS.lower()}/alex"),
    (f"C:{BS}Users{BS}alex{BS}Desktop", f"c:{BS}users{BS}alex"),
    (f'"C:{BS}{BS}Users{BS}{BS}alex"', f"c:{BS}{BS}users{BS}{BS}alex"),
    (f"C:{SL}Users{SL}alex{SL}x", f"c:{SL}users{SL}alex"),
    (f"{PCT_SL}home{PCT_SL}alex{PCT_SL}notes", f"{HOME}/alex"),
    (f"/mnt/c{USERS}/alexq/notes", f"/mnt/c{USERS.lower()}/alexq"),
    (f"cd /MNT/d{USERS.lower()}/alexq", f"/mnt/d{USERS.lower()}/alexq"),
    (f"v{dotted(10, 1, 2, 3)}", dotted(10, 1, 2, 3)),
])
def test_home_paths_and_addresses_in_their_other_spellings(tmp_path, gate, text, match):
    r = scan(gate, tmp_path, {"src/a.txt": f"{text}\n"}, rc=1, parts="d")
    kind = "private-ip" if match[0].isdigit() else "home-path"
    assert hit("d", kind, "src/a.txt", match) in r.lines


def test_percent_encoded_email_is_a_generic_finding(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/a.md": f"mailto:bob{PCT_AT}otherplace.internal\n"}, rc=1,
             parts="d")
    assert hit("d", "email", "docs/a.md", OTHER) in r.lines


def test_lockfile_hosts_come_only_from_uv_lock_url_registry_and_git_sources(tmp_path, gate):
    uv = ('[[package]]\nsource = { registry = "https://pypi.org/simple" }\n'
          'sdist = { url = "https://files.pythonhosted.org/x.tar.gz" }\n[[package]]\n'
          'source = { registry = "https://mirror.samplecorp.test/simple" }\n'
          'wheels = [{ url = "https://wheels.samplecorp.test/x.whl" }]\n[[package]]\n'
          'source = { git = "https://git.samplecorp.test/x?rev=1#abc" }\n'
          'homepage = "https://home.samplecorp.test/"\n')
    lock = {"packages": {"node_modules/b": {"resolved": "https://npm.samplecorp.test/b.tgz"}}}
    files = {"app/package-lock.json": json.dumps(lock), "uv.lock": uv, "nested/uv.lock": uv}
    r = scan(gate, tmp_path, files, rc=1, parts="d")
    want = [hit("d", "lock-host", path, host) for path in ("uv.lock", "nested/uv.lock")
            for host in ("mirror.samplecorp.test", "wheels.samplecorp.test",
                         "git.samplecorp.test")]
    assert sorted(r.lines[:-1]) == sorted(want)


def test_dates_are_not_searched_in_lockfiles(tmp_path, gate):
    files = {"uv.lock": 'version = "2024-03-17"\n', "data/other.json": '{"v": "2024-03-17"}\n',
             "app/package-lock.json": '{"x": "2024-03-17"}\n'}
    r = scan(gate, tmp_path, files, rc=1, parts="c")
    assert sorted(r.lines) == sorted([
        hit("c", "date", "app/package-lock.json", "2024-03-17"),
        hit("c", "date", "data/other.json", "2024-03-17"), "findings: 2 (accepted: 0)"])


AIRLINE_FILES = {
    "docs/ok.md": f"from trips{AT}airline.internal and x{AT}t.AIRLINE.internal\n",
    "docs/no.md": (f"from y{AT}notairline.internal, z{AT}airline.internal.evil.internal "
                   f"and {OTHER}\n"),
}


def test_an_address_at_an_allowed_domain_or_below_it_is_no_generic_finding(tmp_path, gate):
    r = scan(gate, tmp_path, AIRLINE_FILES, rc=1, parts="d", allow_domains=["airline.internal"])
    assert sorted(r.lines[:-1]) == sorted(
        hit("d", "email", "docs/no.md", f"{a}{AT}{b}")
        for a, b in (("y", "notairline.internal"), ("z", "airline.internal.evil.internal"),
                     ("bob", "otherplace.internal")))


def test_without_allow_domains_those_addresses_are_findings(tmp_path, gate):
    r = scan(gate, tmp_path, AIRLINE_FILES, rc=1, parts="d")
    assert len(r.lines) - 1 == 5 and r.report["summary"]["findings"] == 5


RESERVED_FILES = {
    "docs/reserved.md": "".join(f"to x{AT}mail.{label}\n" for label in (
        "test", "example", "invalid", "localhost", "TEST")),
    "docs/private.md": "".join(f"to x{AT}nas.{label}\n" for label in ("local", "lan", "internal")),
}


def test_part_d_lets_an_address_under_a_reserved_top_level_label_pass(tmp_path, gate):
    r = scan(gate, tmp_path, RESERVED_FILES, rc=1, parts="d")
    assert sorted(r.lines[:-1]) == sorted(
        hit("d", "email", "docs/private.md", f"x{AT}nas.{label}")
        for label in ("local", "lan", "internal"))


def test_a_denylisted_address_under_a_reserved_label_is_still_found(tmp_path, gate):
    # ADDR lies under .test: part d lets it pass, part c still finds it as a token
    r = scan(gate, tmp_path, {"docs/a.md": f"write to {ADDR}\n"}, rc=1, parts="c,d")
    assert hit("c", "email", "docs/a.md", ADDR) in r.lines
    assert not [line for line in r.lines if line.startswith("d ")], r.lines


@pytest.mark.parametrize("line", ["two words.test", f"{AT}airline.test", "nodot", "a/b.test"])
def test_a_bad_allow_domains_line_is_an_error(tmp_path, gate, line):
    d = write_deny(tmp_path)
    (d / "allow-domains.txt").write_text(f"# comment\n\nairline.test\n{line}\n")
    r = gate("scan", "--tree", tree(tmp_path, {"a.md": "x\n"}), "--denylist", d, rc=2)
    assert "allow-domains.txt, line 4" in r.err


GENERIC_TEXT = (f"ip {dotted(192, 168, 1, 10)} and {HOME}/alex/notes "
                f"and ip {dotted(10, 0, 0, 7)}\n")
GENERIC_FILES = {f"{path}/net.md": GENERIC_TEXT for path in ("docs", "src")}


def test_allow_generic_drops_a_kind_in_the_paths_it_names(tmp_path, gate):
    allow = [("private-ip", "docs/*"), ("home-path", "docs/*")]
    r = scan(gate, tmp_path, GENERIC_FILES, rc=1, parts="d", allow_generic=allow)
    assert sorted(r.lines[:-1]) == sorted([
        hit("d", "private-ip", "src/net.md", dotted(192, 168, 1, 10)),
        hit("d", "private-ip", "src/net.md", dotted(10, 0, 0, 7)),
        hit("d", "home-path", "src/net.md", f"{HOME}/alex")])


def test_allow_generic_is_per_kind(tmp_path, gate):
    r = scan(gate, tmp_path, {"docs/net.md": GENERIC_FILES["docs/net.md"]}, rc=1, parts="d",
             allow_generic=[("private-ip", "*")])
    assert r.lines == [hit("d", "home-path", "docs/net.md", f"{HOME}/alex"),
                       "findings: 1 (accepted: 0)"]


def test_what_allow_generic_drops_is_counted_as_allowed(tmp_path, gate):
    body = f"ip {dotted(192, 168, 1, 10)}\n"
    r = scan(gate, tmp_path, {"docs/net.md": body, "src/net.md": body}, rc=1, parts="d",
             allow_generic=[("private-ip", "docs/*")])
    assert r.lines == [hit("d", "private-ip", "src/net.md", dotted(192, 168, 1, 10)),
                       "findings: 1 (accepted: 0)"]
    assert r.report["summary"]["allowed"] == 1
    r = scan(gate, tmp_path / "all", {"docs/net.md": body}, rc=0, parts="d",
             allow_generic=[("private-ip", "docs/*")])
    assert r.report["summary"]["allowed"] == 1 and r.report["findings"] == []


def test_without_allow_generic_every_path_is_a_finding(tmp_path, gate):
    r = scan(gate, tmp_path, GENERIC_FILES, rc=1, parts="d")
    assert len(r.lines) - 1 == 6


@pytest.mark.parametrize("line", ["email\tdocs/*", "lock-host\tdocs/*", "private-ip", "home-path\t",
                                  "private-ip\tdocs/*\textra", "name\tdocs/*"])
def test_a_bad_allow_generic_line_is_an_error(tmp_path, gate, line):
    d = write_deny(tmp_path)
    (d / "allow-generic.tsv").write_text(f"# comment\n{line}\n")
    r = gate("scan", "--tree", tree(tmp_path, {"a.md": "x\n"}), "--denylist", d, rc=2)
    assert "allow-generic.tsv, line 2" in r.err


# --- error paths: exit 2, and nothing from the denylist is printed -------------------------------

@pytest.mark.parametrize("raw", [
    f"identifier {ID}\n",
    "Samplefamily\tname\n",
    "name\tSamplefamily\textra\n",
    "colour\tSamplefamily\n",
    "name\t\n",
    "# only a comment\n\n",
    "amount\t4321.09\n",
    "last4\t1234\n",
])
def test_a_malformed_denylist_is_an_error(tmp_path, gate, raw):
    r = scan(gate, tmp_path, {"a.md": "Samplefamily\n"}, rc=2, raw=raw)
    assert r.out == "" and r.err.startswith("error: ")


def test_a_bad_denylist_file_is_named_by_number_never_by_name(tmp_path, gate):
    d = write_deny(tmp_path)
    USED.add("Another Token")
    (d / "Samplefamily.tsv").write_text("name\tAnother Token\nbroken line\n")
    r = gate("scan", "--tree", tree(tmp_path, {"a.md": "x"}), "--denylist", d, rc=2)
    assert "denylist file #1, line 2" in r.err
    (d / "Samplefamily.tsv").write_bytes(b"name\tSample\xfffamily\n")
    r = gate("scan", "--tree", tree(tmp_path, {"a.md": "x"}), "--denylist", d, rc=2)
    assert "denylist file #1" in r.err


@pytest.mark.parametrize("name,body", [
    ("allow-paths.tsv", "name\tSamplefamily\n"),
    ("allow-paths.tsv", "colour\tSamplefamily\tdocs/*\n"),
    ("accepted.tsv", "Samplefamily\tnotes.md\n"),
    ("accepted.tsv", f"{lab(ID)}\n"),
    ("accepted.tsv", f"{hashlib.sha256(ID.encode()).hexdigest()}\tnotes.md\n"),
])
def test_a_malformed_allowance_file_is_an_error(tmp_path, gate, name, body):
    d = write_deny(tmp_path)
    (d / name).write_text(body)
    gate("scan", "--tree", tree(tmp_path, {"a.md": "x"}), "--denylist", d, rc=2)


UNEXPECTED = ("denylist directory holds {} unexpected entries (only *.tsv, allow-vendors.txt, "
              "allow-domains.txt and hmac.key belong there)")


@pytest.mark.parametrize("entry", ["manual.txt", "notes", ".hidden", ".manual.tsv", "manual.tsv~",
                                   "old/", "more.tsv/", "Samplefamily.txt", "allow-generic.txt",
                                   "allow-domains.tsv.bak", "combos.cfg"])
def test_an_unexpected_entry_in_the_denylist_directory_is_an_error(tmp_path, gate, entry):
    d = write_deny(tmp_path)
    USED.add("Quillfeather")
    if entry.endswith("/"):
        (d / entry).mkdir()
        (d / entry / "x.tsv").write_text("name\tQuillfeather\n")
    else:
        (d / entry).write_text("name\tQuillfeather\n")
    r = gate("scan", "--tree", tree(tmp_path, {"a.md": "Quillfeather\n"}), "--denylist", d, rc=2)
    assert r.out == "" and r.err == f"error: {UNEXPECTED.format(1)}\n"


def test_unexpected_entries_are_counted_never_named(tmp_path, gate):
    d = write_deny(tmp_path)
    for name in ("manual.txt", "Samplefamily", f"{ID}.tsv.bak"):
        (d / name).write_text("name\tSamplefamily\n")
    r = gate("scan", "--tree", tree(tmp_path, {"a.md": "x\n"}), "--denylist", d, rc=2)
    assert r.err == f"error: {UNEXPECTED.format(3)}\n"


def test_the_denylist_directory_s_expected_files_load(tmp_path, gate):
    d = tmp_path / "deny"
    d.mkdir()
    (d / "generated.tsv").write_text(
        f"identifier\t{ID}\nvendor\tQuuxair\nplace\t12 Example Way\ncombo\t{COMBO_A}\n")
    (d / "manual.tsv").write_text("name\tSamplefamily\n")
    (d / "allow-paths.tsv").write_text("name\tsamplefamily\tdocs/*\n")
    (d / "accepted.tsv").write_text(f"{lab(ID)}\tnotes.md\n")
    (d / "allow-vendors.txt").write_text("quuxair\n")
    (d / "allow-domains.txt").write_text("# senders\nairline.test\n")
    (d / "allow-generic.tsv").write_text("# docs\nprivate-ip\tdocs/*\n")
    (d / "hmac.key").write_bytes(KEY)
    (d / "hmac.key").chmod(0o600)
    files = {"notes.md": f"{ID}\n", "docs/a.md": f"Samplefamily {dotted(10, 0, 0, 7)}\n",
             "src/a.md": "Samplefamily Quuxair\n", "src/b.md": f"{BOTH}trips{AT}airline.test\n"}
    r = gate("scan", "--tree", tree(tmp_path, files), "--denylist", d, rc=1)
    assert r.lines == [hit("c", "name", "src/a.md", "Samplefamily"),
                       hit("c", "combo", "src/b.md", COMBO_A),
                       "accepted " + hit("c", "identifier", "notes.md", ID),
                       "findings: 2 (accepted: 1)"]
    assert r.report["summary"]["denylist_files"] == 2


def test_missing_or_empty_denylist_and_bad_parts_are_errors(tmp_path, gate):
    t = tree(tmp_path, {"a.md": "x"})
    gate("scan", "--tree", t, "--denylist", tmp_path / "absent", rc=2)
    (tmp_path / "empty").mkdir()
    gate("scan", "--tree", t, "--denylist", tmp_path / "empty", rc=2)
    gate("scan", "--tree", t, "--denylist", write_deny(tmp_path), "--parts", "c,x", rc=2)
    gate("scan", "--tree", tmp_path / "no-tree", "--denylist", write_deny(tmp_path), rc=2)


def test_an_unreadable_file_is_an_error_naming_no_token(tmp_path, capsys, monkeypatch):
    t = tree(tmp_path, {"Samplefamily/locked.md": "x", "ok.md": "y"})
    real = pg._open

    def opener(p):
        if p.name == "locked.md":
            raise PermissionError(13, "Permission denied", str(p))
        return real(p)
    monkeypatch.setattr(pg, "_open", opener)
    rc = pg.main(["scan", "--tree", str(t), "--denylist", str(write_deny(tmp_path))])
    cap = capsys.readouterr()
    assert rc == 2 and f"cannot read [hmac:{lab('Samplefamily')}]/locked.md" in cap.err
    assert_no_token(cap.out + cap.err)


def test_an_unreadable_file_under_a_part_of_a_combo_prints_the_part_redacted(
        tmp_path, capsys, monkeypatch):
    t = tree(tmp_path, {"Robin Sample/locked.md": "x"})
    real = pg._open

    def opener(p):
        if p.name == "locked.md":
            raise PermissionError(13, "Permission denied", str(p))
        return real(p)
    monkeypatch.setattr(pg, "_open", opener)
    rc = pg.main(["scan", "--tree", str(t), "--denylist", str(write_deny(tmp_path))])
    cap = capsys.readouterr()
    assert rc == 2 and f"cannot read [hmac:{lab('Robin Sample')}]/locked.md" in cap.err
    assert_no_token(cap.out + cap.err)


def test_an_unexpected_error_prints_its_type_but_never_its_message(tmp_path, capsys, monkeypatch):
    def boom(self, tree):
        raise ValueError(f"leaked {ID} Samplefamily Robin Sample QQ4X7P")
    monkeypatch.setattr(pg.Scanner, "scan_tree", boom)
    rc = pg.main(["scan", "--tree", str(tree(tmp_path, {"a.md": "x"})), "--denylist",
                  str(write_deny(tmp_path))])
    cap = capsys.readouterr()
    assert rc == 2 and "ValueError" in cap.err
    assert_no_token(cap.out + cap.err)


def test_every_failing_fixture_together_prints_no_token(tmp_path, gate):
    h = ID.encode().hex()
    files = {
        "Samplefamily/notes-pnr9k2m4x7.md": f"{ID} {ADDR} trips.samplefamily.test Quuxair\n",
        "data/rows.csv": "2024-03-17,12 Example Way,Tamarind Lodge\n",
        "src/enc.py": (f"a = '{h}'\nb = '{base64.b64encode(ID.encode()).decode()}'\n"
                       f"c = 'alex{PCT_AT}samplefamily.test'\n"),
        "src/esc.html": "Zo&#235; Quillon \\u0053amplefamily\n",
        "assets/blob.bin": b"\x00" + ID.encode("utf-16-le") + b"\x00",
        "docs/le.txt": b"\xff\xfe" + "Zoë Quillon".encode("utf-16-le"),
        "dist/Samplefamily.zip": zip_bytes({"Alexandria Quill/2024-03-17.csv": "x\n"}),
        "dist/x.txt.xz": lzma.compress(ID.encode()),
        "icons/a.png": png_bytes((b"tEXt", b"Author\x00Samplefamily")),
        "src/net.py": f"'{dotted(192, 168, 1, 1)}' '{HOME}/samplefamily/' '{ADDR}'\n",
        # combos: both parts in one place, in names, in members, in other encodings and layouts
        "docs/Robin Sample QQ4X7P.md": f"{BOTH}Casa Zorblax, Alex Sample and ZZPNR1\n",
        "docs/QQ4X7P/Casa-Zorblax/notes.md": f"{BOTH}Casa Zorblax\tAlex Sample\tZZPNR1\n",
        "dist/Robin-Sample.zip": zip_bytes({
            "Alex Sample/ZZPNR1.txt": "Casa Zorblax Alex Sample ZZPNR1",
            "QQ4X7P/Robin Sample.txt": BOTH}),
        "dist/combo.tar.gz": gzip.compress(tar_bytes({"Casa Zorblax/a.md": BOTH})),
        "assets/combo.bin": b"\x00" + b"Robin Sample".ljust(14) + b"\x00" + b"QQ4X7P".ljust(8),
        "src/combo.ts": "const RobinSample_QQ4X7P = 'Casa Zorblax AlexSample ZZPNR1';\n",
        "docs/combo-enc.md": "Robin%20Sample QQ4X7P and the hex "
                             f"{b'QQ4X7P'.hex()} {base64.b64encode(b'ZZPNR1').decode()}\n",
    }
    r = scan(gate, tmp_path, files, rc=1)
    assert len(r.report["findings"]) == int(r.lines[-1].split()[1]) > 25
    assert len([f for f in r.report["findings"] if f["kind"] == "combo"]) >= 6
    for f in r.report["findings"]:
        assert set(f) == {"part", "kind", "path", "token_hmac", "count", "line", "counts"}
        assert f["count"] == sum(f["counts"].values())
        assert f["token_hmac"] is None or re.fullmatch(r"[0-9a-f]{12}", f["token_hmac"])
    # the combo findings carry the label of the whole token only, never of a part
    labels = {f["token_hmac"] for f in r.report["findings"] if f["kind"] == "combo"}
    assert labels <= {lab(COMBO_A), lab(COMBO_B)}


# --- (a) export ----------------------------------------------------------------------------------

MANIFEST = {
    "allowed_extensions": [".py", ".md", ".txt"],
    "allowed_bare_names": ["Makefile", ".gitattributes"],
    "top_level": ["README.md", "src", "Makefile", ".gitattributes"],
    "forbidden_paths": ["src/private"],
    "executables": ["src/run.py"],
}


def clean_export(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("hello\n")
    (root / "Makefile").write_text("all:\n")
    (root / ".gitattributes").write_text("*.py text\n")
    (root / "src/app.py").write_text("print('x')\n")
    (root / "src/edge.txt").write_bytes(b"x" * 1_048_576)
    (root / "src/run.py").write_text("#!/usr/bin/env python3\n")
    (root / "src/run.py").chmod(0o755)
    return root


def _add_export_subst(r):
    (r / ".gitattributes").write_text("*.txt export-subst\n")


def _add_symlink(r):
    os.symlink("app.py", r / "src/link.py")


def _add_gitmodules(r):
    (r / "src/.gitmodules").write_text("")


def _add_large(r):
    (r / "src/big.txt").write_bytes(b"x" * 1_048_577)


def _add_csv(r):
    (r / "src/data.csv").write_text("a\n")


def _add_bare(r):
    (r / "src/LICENSE").write_text("a\n")


def _add_top(r):
    (r / "extra.md").write_text("a\n")


def _drop_top(r):
    (r / "Makefile").unlink()


def _add_forbidden(r):
    (r / "src/private").mkdir()
    (r / "src/private/x.py").write_text("a\n")


def _add_exec(r):
    (r / "src/app.py").chmod(0o755)


def _drop_exec(r):
    (r / "src/run.py").chmod(0o644)


@pytest.mark.parametrize("mutate,expected", [
    (_add_export_subst, "a export-subst .gitattributes"),
    (_add_symlink, "a symlink src/link.py"),
    (_add_gitmodules, "a gitmodules src/.gitmodules"),
    (_add_large, "a large-file src/big.txt"),
    (_add_csv, "a extension src/data.csv"),
    (_add_bare, "a bare-name src/LICENSE"),
    (_add_top, "a top-level-extra extra.md"),
    (_drop_top, "a top-level-missing Makefile"),
    (_add_forbidden, "a forbidden-path src/private/x.py"),
    (_add_exec, "a executable-extra src/app.py"),
    (_drop_exec, "a executable-missing src/run.py"),
])
def test_each_export_rule_has_a_finding(tmp_path, mutate, expected):
    out = clean_export(tmp_path / "out")
    assert pg.check_export_tree(out, MANIFEST) == []
    mutate(out)
    assert expected in [f.render() for f in pg.check_export_tree(out, MANIFEST)]


def test_export_findings_redact_paths_when_given_a_denylist(tmp_path):
    out = clean_export(tmp_path / "out")
    (out / "src/Samplefamily.csv").write_text("a\n")
    (out / "src/QQ4X7P.csv").write_text("a\n")
    deny = pg.Denylist.load(write_deny(tmp_path))
    lines = [f.render() for f in pg.check_export_tree(out, MANIFEST, deny=deny)]
    assert f"a extension src/[hmac:{lab('Samplefamily')}].csv" in lines
    assert f"a extension src/[hmac:{lab('QQ4X7P')}].csv" in lines
    assert_no_token("\n".join(lines))


@pytest.mark.parametrize("change", [
    lambda m: m.pop("executables"),
    lambda m: m.update(extra=[]),
    lambda m: m.update(top_level="README.md"),
])
def test_a_malformed_manifest_is_an_error(tmp_path, gate, change):
    manifest = json.loads(json.dumps(MANIFEST))
    change(manifest)
    (tmp_path / "m.json").write_text(json.dumps(manifest))
    r = gate("export", "--repo", tmp_path, "--ref", "HEAD", "--out", tmp_path / "out",
             "--manifest", tmp_path / "m.json", "--denylist", write_deny(tmp_path), rc=2)
    assert "manifest" in r.err and not (tmp_path / "out").exists()


GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Alex Sample", "GIT_AUTHOR_EMAIL": "alex@example.com",
           "GIT_COMMITTER_NAME": "Alex Sample", "GIT_COMMITTER_EMAIL": "alex@example.com"}


def git_repo(tmp_path: Path, mutate=None) -> Path:
    repo = clean_export(tmp_path / "repo")
    if mutate:
        mutate(repo)
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=GIT_ENV,
                       capture_output=True)
    return repo


def export(gate, tmp_path, repo, manifest=MANIFEST, ref="HEAD", rc=None, out=None,
           denylist=True) -> Result:
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    args = ["export", "--repo", repo, "--ref", ref, "--out", out or tmp_path / "out",
            "--manifest", tmp_path / "manifest.json"]
    return gate(*args, *(["--denylist", write_deny(tmp_path)] if denylist else []), rc=rc)


def test_export_of_a_clean_repo_exits_zero(tmp_path, gate):
    r = export(gate, tmp_path, git_repo(tmp_path), rc=0)
    assert r.lines == ["findings: 0 (accepted: 0)"]
    assert os.access(tmp_path / "out/src/run.py", os.X_OK)


def test_export_requires_a_denylist(tmp_path, gate):
    r = export(gate, tmp_path, git_repo(tmp_path), denylist=False, rc=2)
    assert "required: --denylist" in r.err and not (tmp_path / "out").exists()


def test_export_finds_export_subst_in_an_export_ignored_gitattributes(tmp_path, gate):
    def hide(repo):
        (repo / ".gitattributes").write_text(".gitattributes export-ignore\n*.txt export-subst\n")
    manifest = {**MANIFEST, "top_level": ["README.md", "src", "Makefile"]}
    r = export(gate, tmp_path, git_repo(tmp_path, hide), manifest, rc=1)
    assert "a export-subst .gitattributes" in r.lines
    assert not (tmp_path / "out/.gitattributes").exists()


def test_export_finds_export_subst_in_the_repository_info_attributes(tmp_path, gate):
    repo = git_repo(tmp_path)
    (repo / ".git/info").mkdir(exist_ok=True)
    (repo / ".git/info/attributes").write_text("*.txt export-subst\n")
    r = export(gate, tmp_path, repo, rc=1)
    assert r.lines == ["a export-subst .git/info/attributes", "findings: 1 (accepted: 0)"]


def test_export_checks_the_extracted_tree(tmp_path, gate):
    r = export(gate, tmp_path, git_repo(tmp_path, _add_exec), rc=1)
    assert "a executable-extra src/app.py" in r.lines


def test_export_refuses_a_non_empty_out_dir_or_a_bad_ref(tmp_path, gate):
    repo = git_repo(tmp_path)
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy/x").write_text("x")
    export(gate, tmp_path, repo, out=tmp_path / "busy", rc=2)
    export(gate, tmp_path, repo, ref="no-such-ref", out=tmp_path / "o1", rc=2)
    export(gate, tmp_path, repo, ref="--output=/tmp/x", out=tmp_path / "o2", rc=2)


# --- (e) images ----------------------------------------------------------------------------------

def save_tar(layers: dict[str, bytes]) -> bytes:
    return tar_bytes({"manifest.json": json.dumps([{"Layers": list(layers)}]), **layers})


def fake_docker(save, env=("PATH=/usr/bin",), labels=None, history='CMD ["python"]'):
    """A docker stand-in: `save -o PATH IMAGE` writes `save` (bytes, or a callable given PATH) and
    records how private the directory holding PATH is; inspect and history return their stdout."""
    seen: dict = {}

    def run(argv: list[str]) -> bytes:
        if argv[:3] == ["docker", "save", "-o"]:
            seen["path"] = argv[3]
            seen["dir_mode"] = stat.S_IMODE(os.stat(os.path.dirname(argv[3])).st_mode)
            if callable(save):
                save(argv[3])
            else:
                Path(argv[3]).write_bytes(save)
            return b""
        if argv[:2] == ["docker", "inspect"]:
            return json.dumps([{"Config": {"Env": list(env), "Labels": labels}}]).encode()
        if argv[:3] == ["docker", "history", "--no-trunc"]:
            return history.encode()
        raise AssertionError("unexpected docker call")
    run.seen = seen
    return run


def images(tmp_path, capsys, run, *names, export_names=("itinerary.json",),
           app_paths=(), rules=()) -> tuple[int, list[str]]:
    """`rules` are (option, glob) pairs, `("--upstream", "*")`, passed in their order"""
    listing = tmp_path / "export-names.txt"
    listing.write_text("# basenames of export files\n" + "\n".join(export_names) + "\n")
    argv = ["images", "--denylist", str(write_deny(tmp_path)), "--export-names", str(listing)]
    for prefix in app_paths:
        argv += ["--app-path", prefix]
    for option, glob in rules:
        argv += [option, glob]
    rc = pg.main([*argv, *(names or ("demo-image:1",))], run=run)
    cap = capsys.readouterr()
    assert_no_token(cap.out + cap.err)
    return rc, [undecorated(line) for line in cap.out.splitlines()]


CLEAN_LAYER = {"app/main.py": "print('hi')\n", "usr/share/x.txt": "plain\n"}


def test_a_clean_image_exits_zero_and_its_save_stays_private_and_temporary(tmp_path, capsys):
    run = fake_docker(save_tar({"l1/layer.tar": tar_bytes(CLEAN_LAYER)}),
                      labels={"org.opencontainers.image.title": "demo"})
    rc, lines = images(tmp_path, capsys, run, export_names=("itinerary.json", "manifest.json"))
    assert (rc, lines) == (0, ["findings: 0 (accepted: 0)"])
    assert run.seen["dir_mode"] == 0o700 and not os.path.exists(os.path.dirname(run.seen["path"]))


@pytest.mark.parametrize("member,expected", [
    ("app/dist/index.js.map", "e source-map demo-image:1!l1/layer.tar!app/dist/index.js.map"),
    ("app/data/exports/itinerary.json",
     "e export-json demo-image:1!l1/layer.tar!app/data/exports/itinerary.json"),
])
def test_image_rules_on_member_names(tmp_path, capsys, member, expected):
    layer = tar_bytes({**CLEAN_LAYER, member: "{}"})
    rc, lines = images(tmp_path, capsys, fake_docker(save_tar({"l1/layer.tar": layer})))
    assert rc == 1 and expected in lines


def test_image_layer_contents_are_scanned_with_tokens_and_generic_rules(tmp_path, capsys):
    oci_blob = gzip.compress(tar_bytes({"app/y.py": f"x = '{ID}'\n"}))
    save = save_tar({"l1/layer.tar": tar_bytes({"app/x.py": f"{ID}\n",
                                                 "etc/conf": f"root={HOME}/alex/x\n"}),
                     "blobs/sha256/0f0f": oci_blob})
    rc, lines = images(tmp_path, capsys, fake_docker(save))
    assert rc == 1
    assert hit("c", "identifier", "demo-image:1!l1/layer.tar!app/x.py", ID) in lines
    assert hit("c", "identifier", "demo-image:1!blobs/sha256/0f0f!app/y.py", ID) in lines
    assert hit("d", "home-path", "demo-image:1!l1/layer.tar!etc/conf", f"{HOME}/alex") in lines


def test_image_combos_are_found_per_member_and_in_env_labels_and_history(tmp_path, capsys):
    layer = tar_bytes({"app/both.txt": BOTH, "app/one.txt": "Robin Sample\n",
                       "app/two.txt": "QQ4X7P\n", "Robin Sample/x.txt": "x\n"})
    run = fake_docker(save_tar({"l1/layer.tar": layer}), env=["OWNER=Robin Sample"],
                      labels={"ref": "QQ4X7P"}, history="RUN echo Casa Zorblax Alex Sample ZZPNR1")
    rc, lines = images(tmp_path, capsys, run)
    assert rc == 1
    assert sorted(line for line in lines if " combo " in line) == sorted([
        hit("c", "combo", "demo-image:1!l1/layer.tar!app/both.txt", COMBO_A),
        hit("c", "combo", "demo-image:1!docker-inspect", COMBO_A),
        hit("c", "combo", "demo-image:1!docker-history", COMBO_B)])


def oci_save(second_layer: bytes) -> bytes:
    real = tar_bytes(CLEAN_LAYER)
    manifest = json.dumps([{"Layers": ["blobs/sha256/aaaa", "blobs/sha256/bbbb"]}])
    return tar_bytes({"manifest.json": manifest, "index.json": "{}",
                      "oci-layout": '{"imageLayoutVersion": "1.0.0"}',
                      "blobs/sha256/aaaa": real, "blobs/sha256/bbbb": second_layer})


def test_an_empty_layer_is_a_valid_layer(tmp_path, capsys):
    assert pg._archive_kind(b"\0" * 1024) is None
    rc, lines = images(tmp_path, capsys, fake_docker(oci_save(b"\0" * 1024)))
    assert (rc, lines) == (0, ["findings: 0 (accepted: 0)"])


def test_a_listed_layer_that_is_neither_an_archive_nor_a_tar_is_unsupported(tmp_path, capsys):
    noise = bytes((i * 167 + 13) % 251 + 1 for i in range(4096))
    assert pg._archive_kind(noise) is None
    rc, lines = images(tmp_path, capsys, fake_docker(oci_save(noise)))
    assert rc == 1 and "e archive-unsupported demo-image:1!blobs/sha256/bbbb" in lines


def test_a_listed_layer_that_is_not_an_archive_or_is_missing_is_a_finding(tmp_path, capsys):
    manifest = json.dumps([{"Layers": ["blobs/sha256/aa", "blobs/sha256/bb"]}])
    save = tar_bytes({"manifest.json": manifest, "blobs/sha256/aa": b"\x00\x01 not an archive"})
    rc, lines = images(tmp_path, capsys, fake_docker(save))
    assert rc == 1
    assert "e archive-unsupported demo-image:1!blobs/sha256/aa" in lines
    assert "e layer-missing demo-image:1!blobs/sha256/bb" in lines


def test_app_paths_limit_the_generic_rules_but_never_the_tokens(tmp_path, capsys):
    layer = tar_bytes({
        "usr/share/doc/x/copyright":
            f"maint{AT}distro.internal {HOME}/builder/x {dotted(10, 0, 0, 9)}\n",
        "app/trip-app/x.py": f"p = '{HOME}/alex/x'\n", "usr/lib/y.py": f"{ID}\n"})
    run = fake_docker(save_tar({"l1/layer.tar": layer}), env=[f"MAIL=ops{AT}elsewhere.internal"])
    rc, lines = images(tmp_path, capsys, run, app_paths=["/app/trip-app/"])
    assert rc == 1
    assert {line for line in lines if line.startswith("d ")} == {
        hit("d", "home-path", "demo-image:1!l1/layer.tar!app/trip-app/x.py", f"{HOME}/alex"),
        hit("d", "email", "demo-image:1!docker-inspect", f"ops{AT}elsewhere.internal")}
    assert hit("c", "identifier", "demo-image:1!l1/layer.tar!usr/lib/y.py", ID) in lines
    rc, lines = images(tmp_path, capsys, run)
    assert hit("d", "email", "demo-image:1!l1/layer.tar!usr/share/doc/x/copyright",
               f"maint{AT}distro.internal") in lines


def upstream_line(findings: int, members: int) -> str:
    return (f"upstream: {findings} findings in {members} members "
            "(public upstream bytes, not counted)")


# a token in each of four members, which the rules below sort one way and then the other
SORTED_LAYER = {"usr/lib/a.py": f"{ID}\n", "app/x.py": f"{ID}\n", "app/vendor/y.py": f"{ID}\n",
                "app/vendor/own/z.py": f"{ID}\n"}


def test_the_last_matching_member_rule_wins(tmp_path, capsys):
    run = fake_docker(save_tar({"l1/layer.tar": tar_bytes(SORTED_LAYER)}))
    rules = [("--upstream", "*"), ("--own", "app/*"), ("--upstream", "app/vendor/*"),
             ("--own", "app/vendor/own/*")]
    rc, lines = images(tmp_path, capsys, run, rules=rules)
    assert rc == 1
    assert [line for line in lines if line.startswith("c ")] == [
        hit("c", "identifier", "demo-image:1!l1/layer.tar!app/vendor/own/z.py", ID),
        hit("c", "identifier", "demo-image:1!l1/layer.tar!app/x.py", ID)]
    assert lines[-2:] == [upstream_line(2, 2), "findings: 2 (accepted: 0)"]
    # the same rules in another order: the catch-all comes last and takes every member
    rc, lines = images(tmp_path, capsys, run, rules=[*rules[1:], rules[0]])
    assert (rc, lines) == (0, [upstream_line(4, 4), "findings: 0 (accepted: 0)"])


def test_the_member_rules_keep_their_command_line_order(tmp_path, capsys):
    args = pg._parser().parse_args(["images", "--denylist", "d", "--export-names", "n",
                                    "--own", "a/*", "--upstream", "*", "--own", "b/*", "img"])
    assert args.rules == [("own", "a/*"), ("upstream", "*"), ("own", "b/*")]
    assert pg.main(["images", "--denylist", "d", "--export-names", "n", "img", "--own"]) == 2
    assert "expected one argument" in capsys.readouterr().err


def test_with_no_member_rule_every_member_is_own_and_no_upstream_line_is_printed(
        tmp_path, capsys):
    run = fake_docker(save_tar({"l1/layer.tar": tar_bytes(SORTED_LAYER)}))
    rc, lines = images(tmp_path, capsys, run)
    assert rc == 1 and lines[-1] == "findings: 4 (accepted: 0)"
    assert not any(line.startswith("upstream:") for line in lines)
    # a rule no member matches changes nothing but the upstream line
    rc, ruled = images(tmp_path, capsys, run, rules=[("--upstream", "srv/*")])
    assert (rc, ruled) == (1, [*lines[:-1], upstream_line(0, 0), lines[-1]])


def test_content_matches_in_upstream_members_do_not_fail_the_scan(tmp_path, capsys):
    layer = tar_bytes({
        "usr/lib/a.py": f"{ID}\n", "usr/share/doc/x/copyright": f"maint{AT}distro.internal\n",
        "opt/both.txt": BOTH,
        "usr/lib/pkg.zip": zip_bytes({"inner/readme.txt": "Samplefamily\n"}),
        "Samplefamily/x.txt": "x\n", "app/main.py": "print('hi')\n"})
    run = fake_docker(save_tar({"l1/layer.tar": layer}))
    rc, lines = images(tmp_path, capsys, run)
    assert rc == 1
    every = int(lines[-1].split()[1])
    assert hit("c", "combo", "demo-image:1!l1/layer.tar!opt/both.txt", COMBO_A) in lines
    rc, lines = images(tmp_path, capsys, run, rules=[("--upstream", "*"), ("--own", "app/*")])
    assert (rc, lines) == (0, [upstream_line(every, 5), "findings: 0 (accepted: 0)"])


def test_image_and_archive_findings_in_upstream_members_still_fail(tmp_path, capsys):
    layer = tar_bytes({
        "usr/share/exports/itinerary.json": f"{ID}\n", "usr/lib/js/x.js.map": "{}",
        "usr/lib/broken.zip": b"PK\x03\x04 not a zip", "app/main.py": "print('hi')\n"})
    run = fake_docker(save_tar({"l1/layer.tar": layer}))
    rc, lines = images(tmp_path, capsys, run, rules=[("--upstream", "*"), ("--own", "app/*")])
    assert rc == 1
    assert [line for line in lines if not line.startswith(("upstream:", "findings:"))] == [
        "c archive-error demo-image:1!l1/layer.tar!usr/lib/broken.zip",
        "e source-map demo-image:1!l1/layer.tar!usr/lib/js/x.js.map",
        "e export-json demo-image:1!l1/layer.tar!usr/share/exports/itinerary.json"]
    # the token in the export file is a content match: counted as upstream, never printed
    assert lines[-2:] == [upstream_line(1, 1), "findings: 3 (accepted: 0)"]


def test_a_finding_in_the_projects_own_files_inside_the_venv_still_fails(tmp_path, capsys):
    site = "app/.venv/lib/python3.14/site-packages"
    layer = tar_bytes({f"{site}/tripbox-1.0.0.dist-info/METADATA": "Summary: pnr9k2m4x7\n",
                       f"{site}/other-2.0.dist-info/METADATA": "Summary: pnr9k2m4x7\n",
                       f"{site}/other/__init__.py": f"{ID}\n", "app/.venv/bin/tripbox": "x\n"})
    run = fake_docker(save_tar({"l1/layer.tar": layer}))
    rules = [("--upstream", "*"), ("--own", "app/*"), ("--upstream", "app/.venv/*"),
             ("--own", "app/.venv/lib/python*/site-packages/tripbox-*")]
    rc, lines = images(tmp_path, capsys, run, rules=rules)
    assert rc == 1
    assert [line for line in lines if line[:2] in ("c ", "d ")] == [
        hit("c", "identifier",
            f"demo-image:1!l1/layer.tar!{site}/tripbox-1.0.0.dist-info/METADATA", "pnr9k2m4x7")]
    assert lines[-2:] == [upstream_line(2, 2), "findings: 1 (accepted: 0)"]


def test_the_upstream_line_names_no_token_label_kind_or_path(tmp_path, capsys):
    layer = tar_bytes({"usr/lib/quill/a.py": "Alexandria Quill pnr9k2m4x7\n",
                       "usr/share/b.txt": f"{HOME}/builder/x\n"})
    run = fake_docker(save_tar({"l1/layer.tar": layer}))
    rc, lines = images(tmp_path, capsys, run, rules=[("--upstream", "usr/*")])
    assert (rc, lines) == (0, [upstream_line(3, 2), "findings: 0 (accepted: 0)"])
    out = "\n".join(lines)
    for token in ("Alexandria Quill", "pnr9k2m4x7"):
        assert lab(token) not in out
    assert not any(word in out for word in ("usr", "quill", "identifier", "home-path", "hmac"))


def test_image_env_labels_and_history_are_scanned(tmp_path, capsys):
    run = fake_docker(save_tar({"l1/layer.tar": tar_bytes(CLEAN_LAYER)}),
                      env=["OWNER=Samplefamily"], labels={"maintainer": ADDR, "contact": OTHER},
                      history="RUN echo pnr9k2m4x7")
    rc, lines = images(tmp_path, capsys, run)
    assert rc == 1
    assert hit("c", "name", "demo-image:1!docker-inspect", "Samplefamily") in lines
    assert hit("c", "email", "demo-image:1!docker-inspect", ADDR) in lines
    assert hit("d", "email", "demo-image:1!docker-inspect", OTHER) in lines
    # ADDR lies under a reserved label: a token for part c, no finding for part d
    assert hit("d", "email", "demo-image:1!docker-inspect", ADDR) not in lines
    assert hit("c", "identifier", "demo-image:1!docker-history", "pnr9k2m4x7") in lines


def test_a_docker_failure_or_unreadable_inspect_is_an_error(tmp_path, capsys):
    def failing(argv):
        raise subprocess.CalledProcessError(1, argv, stderr=f"no such image {ID}".encode())
    assert images(tmp_path, capsys, failing)[0] == 2
    good = fake_docker(save_tar({"l1/layer.tar": tar_bytes(CLEAN_LAYER)}))
    assert images(tmp_path, capsys,
                  lambda argv: b"not json" if argv[1] == "inspect" else good(argv))[0] == 2
    assert not os.path.exists(os.path.dirname(good.seen["path"]))


class Filler:
    """`size` bytes of 8 KiB dotted lines with `token` written at offset `at`, produced as read;
    with no blank near the token, a cut falls inside it and only the overlap between pieces keeps
    it whole"""

    def __init__(self, size: int, token: bytes, at: int) -> None:
        self.size, self.token, self.at, self.pos = size, token, at, 0

    def read(self, n: int = -1) -> bytes:
        n = self.size - self.pos if n < 0 else min(n, self.size - self.pos)
        start = self.pos
        out = bytearray(((b"." * 8191 + b"\n") * (n // 8192 + 2))[start % 8192:start % 8192 + n])
        lo, hi = max(start, self.at), min(start + n, self.at + len(self.token))
        if lo < hi:
            out[lo - start:hi - start] = self.token[lo - self.at:hi - self.at]
        self.pos += n
        return bytes(out)


def test_a_large_member_is_scanned_from_disk_in_bounded_memory(tmp_path, capsys):
    size = 80 << 20

    def write_save(path: str) -> None:
        layer = tmp_path / "layer.tar"
        with tarfile.open(layer, "w") as t:
            info = tarfile.TarInfo("app/big.log")
            info.size = size
            t.addfile(info, Filler(size, f".{ID}.".encode(), pg.CHUNK - 8))
        manifest = json.dumps([{"Layers": ["l1/layer.tar"]}]).encode()
        with tarfile.open(path, "w") as t:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest)
            t.addfile(info, io.BytesIO(manifest))
            t.add(layer, arcname="l1/layer.tar")
        layer.unlink()
    tracemalloc.start()
    try:
        rc, lines = images(tmp_path, capsys, fake_docker(write_save))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    print(f"\npeak traced memory scanning an 80 MiB member: {peak / 2**20:.1f} MiB")
    assert rc == 1
    assert hit("c", "identifier", "demo-image:1!l1/layer.tar!app/big.log", ID) in lines
    assert peak < 300 * 2**20


# --- speed: a realistic denylist over a realistic tree -------------------------------------------

def _word(i: int) -> str:
    s = ""
    for _ in range(4):
        i, r = divmod(i, 26)
        s += chr(ord("a") + r)
    return s


def test_ten_thousand_tokens_over_hundreds_of_files_finish_well_inside_the_bound(tmp_path):
    classes = ("identifier", "name", "vendor", "email", "host", "date", "place", "combo")
    rows = []
    for i in range(10_000):
        cls = classes[i % 8]
        rows.append((cls, {
            "identifier": f"syn_ref_{i:06d}", "name": f"Zq{_word(i)}son",
            "vendor": f"Store {_word(i)} Mart", "email": f"user{i}{AT}synthetic{i % 50}.test",
            "host": f"host{i}.synthetic.test",
            "date": f"20{10 + i % 15}-{1 + i % 12:02d}-{1 + i % 28:02d}",
            "place": f"{i} Synthetic Way", "combo": f"Zr{_word(i)}ly{SEP}ref{i:06d}"}[cls]))
    d = tmp_path / "deny"
    d.mkdir()
    (d / "tokens.tsv").write_text("".join(f"{c}\t{t}\n" for c, t in rows), encoding="utf-8")
    files = {}
    for n in range(300):
        files[f"src/m{n:03d}.py"] = "".join(
            f"def f{n}_{k}(x):\n    return x * {k}.5 + {n}  # value {k} of the store mart\n"
            for k in range(40))
    files["src/m017.py"] += "REF = 'syn_ref_000008'\n"
    files["src/m200.py"] += "# bought at Store caaa Mart\n"
    files["src/m201.py"] += "# Zrhaaaly and ref000007\n"
    integrity = base64.b64encode(hashlib.sha512(b"x").digest()).decode()
    lock = {f"node_modules/p{i}": {"version": f"{i % 9}.{i % 13}.{i % 7}",
                                   "resolved": f"https://registry.npmjs.org/p{i}/-/p{i}-1.0.0.tgz",
                                   "integrity": "sha512-" + integrity}
            for i in range(4000)}
    files["app/package-lock.json"] = json.dumps({"packages": lock}, indent=2)
    files["uv.lock"] = "".join(
        f'{{ url = "https://files.pythonhosted.org/p{i}.whl", hash = "sha256:'
        f'{hashlib.sha256(str(i).encode()).hexdigest()}" }}\n' for i in range(4000))
    t = tree(tmp_path, files)
    start = time.monotonic()
    deny = pg.Denylist.load(d)
    found = pg.scan_tree(t, deny, {"b", "c", "d"})
    elapsed = time.monotonic() - start
    print(f"\n10,000 tokens over {len(files)} files: {elapsed:.2f}s")
    assert elapsed < 60
    lines = [undecorated(f.render()) for f in found]
    key = (d / "hmac.key").read_bytes()
    assert hit("c", "identifier", "src/m017.py", "syn_ref_000008").replace(
        lab("syn_ref_000008"), lab("syn_ref_000008", key)) in lines
    assert f"c vendor src/m200.py hmac:{lab('Store caaa Mart', key)}" in lines
    combo = f"Zrhaaaly{SEP}ref000007"
    assert f"c combo src/m201.py hmac:{lab(combo, key)}" in lines

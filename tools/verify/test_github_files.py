# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The files GitHub reads (the issue forms, the pull-request template) and the contributor's
guide: the parse-problem form refuses an original email and asks for the report, blank issues are
off and point at the security policy, and the guide names nothing private and stays in budget."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
# The contributor's guide: the one path that changes when the guide replaces AGENTS.md (the export
# rewrites it, and every other mention of it, to AGENTS.md).
CONTRIBUTORS_FILE = "AGENTS.md"
BUDGETS = {
    CONTRIBUTORS_FILE: 36_000,
    "src/tripbox/web/AGENTS.md": 9_000,
    "src/tripbox/write/AGENTS.md": 9_000,
}
# Until the guide replaces it, AGENTS.md is a file of its own with a budget of its own.
if CONTRIBUTORS_FILE != "AGENTS.md":
    BUDGETS["AGENTS.md"] = 37_000
# Words that name the maintainers' own host or another project, which the guide never carries.
PRIVATE = ("tools/private", "tripboxoss", "~/", "travelbox", "household", "tripbox-dev")
HOST_PORTS = re.compile(r"\b183[0-2][0-9]\b|\b18330\b")


def form(name: str) -> dict:
    text = (FORMS / name).read_text(encoding="utf-8")
    return yaml.safe_load(text)


def test_the_parse_problem_form_asks_for_the_report_and_refuses_an_original_email():
    f = form("parse-problem.yml")
    first = f["body"][0]
    assert first["type"] == "markdown"
    assert first["attributes"]["value"].startswith("Never attach an original email.")
    fields = {item["id"]: item for item in f["body"] if "id" in item}
    assert set(fields) == {"expected", "filed", "report", "version", "reader", "checked"}
    for key in ("expected", "filed", "report", "version", "reader"):
        assert fields[key]["validations"]["required"] is True
    assert fields["report"]["attributes"]["render"] == "text"
    assert "tripbox report" in fields["report"]["attributes"]["description"]
    boxes = fields["checked"]["attributes"]["options"]
    assert boxes == [
        {
            "label": "I read the report and it holds nothing I do not want public.",
            "required": True,
        }
    ]
    assert f["labels"] == ["parse-problem"]


def test_the_bug_form_asks_for_doctors_output_and_says_it_prints_no_secret():
    f = form("bug.yml")
    doctor = next(item for item in f["body"] if item.get("id") == "doctor")
    assert doctor["validations"]["required"] is True
    assert "tripbox doctor" in doctor["attributes"]["description"]
    assert "prints no secret" in doctor["attributes"]["description"]


def test_blank_issues_are_off_and_the_security_policy_is_linked():
    f = form("config.yml")
    assert f["blank_issues_enabled"] is False
    urls = [link["url"] for link in f["contact_links"]]
    assert urls == ["https://github.com/Good-Work-Dojo/tripbox/security/policy"]
    assert (ROOT / "SECURITY.md").is_file()


def test_the_pull_request_template_asks_three_things():
    boxes = re.findall(
        r"^- \[ \] \*\*(.+?)\.\*\*", (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(), re.M
    )
    assert boxes == ["No real data", "The gate ran", "The agreement"]


def test_every_form_carries_the_licence_header():
    for path in sorted(FORMS.glob("*.yml")):
        head = path.read_text(encoding="utf-8").splitlines()[:2]
        assert head == [
            "# SPDX-License-Identifier: AGPL-3.0-only",
            "# Additional terms: see NOTICE",
        ], path.name


def test_the_contributors_file_names_nothing_private():
    text = (ROOT / CONTRIBUTORS_FILE).read_text(encoding="utf-8")
    for word in PRIVATE:
        assert word not in text, word
    assert not HOST_PORTS.search(text)
    # the two nested files are named, and no import line stands in the guide
    assert "src/tripbox/web/AGENTS.md" in text and "src/tripbox/write/AGENTS.md" in text
    assert not re.search(r"^@\S+", text, re.M)


def test_the_context_files_are_within_their_byte_budgets():
    for name, limit in BUDGETS.items():
        size = len((ROOT / name).read_bytes())
        assert size <= limit, (name, size, limit)

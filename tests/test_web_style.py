# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The stylesheet: the font it ships with the page, nothing it names from elsewhere, and text
colours that read on their backgrounds in both colour schemes."""

import re
import shutil
from pathlib import Path

import tripbox.web
from tripbox.web import app as webapp

STATIC = Path(tripbox.web.__file__).resolve().parent / "static"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
# The four upright files of the bundled font, by the weight each one is
WEIGHTS = {"400": "Regular", "500": "Medium", "600": "SemiBold", "700": "Bold"}


def declarations(block: str) -> dict[str, str]:
    """The `name: value` pairs of one rule's body."""
    pairs = (d.split(":", 1) for d in block.split(";") if ":" in d)
    return {name.strip(): value.strip() for name, value in pairs}


def test_the_font_ships_with_the_page_one_file_per_weight():
    faces = [declarations(body) for body in re.findall(r"@font-face\s*\{([^}]*)\}", CSS)]
    assert sorted(face["font-weight"] for face in faces) == sorted(WEIGHTS)
    for face in faces:
        weight = face["font-weight"]
        assert face["font-family"] == '"Public Sans"', weight
        assert (face["font-style"], face["font-display"]) == ("normal", "swap"), weight
        assert face["src"] == f'url("fonts/PublicSans-{WEIGHTS[weight]}.woff2") format("woff2")'
    assert re.search(r'--font:\s*"Public Sans",', CSS)
    # every url() names a file beside the stylesheet, by a path relative to it
    urls = re.findall(r"url\(\s*['\"]?([^'\")]+)", CSS)
    assert len(urls) == len(WEIGHTS)
    for url in urls:
        assert "://" not in url and not url.startswith(("/", "data:")), url
        assert (STATIC / url).is_file(), url
    # no weight or style the four files do not have
    weights = re.findall(r"font-weight:\s*([^;}\s]+)", CSS)
    assert weights and set(weights) <= set(WEIGHTS), set(weights) - set(WEIGHTS)
    assert not re.search(r"font-style:\s*(italic|oblique)", CSS)
    for shorthand in re.findall(r"\bfont:\s*([^;}]+)", CSS):
        assert not re.search(r"\b(bold|bolder|lighter|italic|oblique|[1-9]00)\b", shorthand)


def test_the_version_the_pages_files_are_asked_for_by_covers_the_fonts(tmp_path):
    static = tmp_path / "static"
    shutil.copytree(STATIC, static)
    before = webapp._asset_version(static)
    assert before == webapp._asset_version(static) and re.fullmatch(r"[0-9a-f]{12}", before)
    font = static / "fonts" / "PublicSans-Bold.woff2"
    font.write_bytes(font.read_bytes() + b"\0")
    assert webapp._asset_version(static) != before


def colours(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """The colour variables of the light scheme and, over them, of the dark one."""
    light = re.search(r":root\s*\{([^}]*)\}", css)
    dark = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{([^}]*)\}", css)
    assert light and dark

    def variables(block: str) -> dict[str, str]:
        return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9a-fA-F]{6})\b", block))

    return variables(light.group(1)), {**variables(light.group(1)), **variables(dark.group(1))}


def luminance(colour: str) -> float:
    """WCAG 2's relative luminance of a #rrggbb colour."""
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# The text colours and the backgrounds they sit on; the button text on its fill and the error
# text on its own background besides
TEXT = ("--fg", "--muted", "--primary", "--red")
GROUNDS = ("--bg", "--card", "--band")
PAIRS = (
    *((t, g) for t in TEXT for g in GROUNDS),
    ("--primary-fg", "--primary"),
    ("--red", "--red-bg"),
)


def test_text_colours_pass_aa_on_their_backgrounds():
    assert round(contrast("#000000", "#ffffff"), 2) == 21 and contrast("#777777", "#777777") == 1
    light, dark = colours(CSS)
    for scheme, value in (("light", light), ("dark", dark)):
        for text, ground in PAIRS:
            ratio = contrast(value[text], value[ground])
            assert ratio >= 4.5, (scheme, text, ground, round(ratio, 2))


def test_the_copy_button_of_a_confirmation_code_is_a_finger_high():
    rule = re.search(r"\.conf\s*\{([^}]*)\}", CSS)
    assert rule and declarations(rule.group(1))["min-height"] == "44px"

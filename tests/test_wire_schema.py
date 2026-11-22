# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The wire schema's shape is pinned per version, so a stored answer is never read under a shape
it was not written for."""

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tripbox.parse import replay
from tripbox.parse.models import wire_shape
from tripbox.parse.replay import WIRE_SCHEMA_VERSION

DATA = Path(__file__).parent / "data"
FORBIDDEN = {"$defs", "$ref", "title", "description"}


def _annotation_keys(node) -> set[str]:
    """Keys at schema positions; the names under `properties` and data values are skipped."""
    if isinstance(node, list):
        return {k for v in node for k in _annotation_keys(v)}
    if not isinstance(node, dict):
        return set()
    keys = set(node)
    for key, value in node.items():
        if key == "properties":
            keys |= {k for sub in value.values() for k in _annotation_keys(sub)}
        elif key not in ("default", "enum", "const", "examples"):
            keys |= _annotation_keys(value)
    return keys


def test_the_wire_shape_is_frozen_at_its_version():
    frozen = json.loads((DATA / "wire_schema" / f"{WIRE_SCHEMA_VERSION}.json").read_text())
    assert wire_shape() == frozen, (
        "the wire schema changed: bump WIRE_SCHEMA_VERSION, add tests/data/wire_schema/<n>.json "
        "and convert or re-record stored answers (a rendering-only change regenerates the "
        "current file)"
    )


def test_every_version_up_to_the_current_one_has_its_frozen_shape():
    names = sorted(p.name for p in (DATA / "wire_schema").iterdir())
    assert names == [f"{n}.json" for n in range(1, WIRE_SCHEMA_VERSION + 1)]


def test_the_shape_ignores_wording_and_names_and_sees_fields_types_and_choices():
    class Inner(BaseModel):
        x: str | None = Field(None, description="one wording")

    class Renamed(BaseModel):
        x: str | None = Field(None, description="another wording")

    class A(BaseModel):
        """a docstring"""

        inner: Inner
        n: str | None = Field(None, description="first")

    class B(BaseModel):
        inner: Renamed
        n: str | None = Field(None, description="second")

    assert wire_shape(A) == wire_shape(B)

    class Added(A):
        extra: int = 0

    class Retyped(BaseModel):
        inner: Inner
        n: int | None = None

    class Pick1(BaseModel):
        k: Literal["a", "b"]

    class Pick2(BaseModel):
        k: Literal["a", "b", "c"]

    assert wire_shape(Added) != wire_shape(A)
    assert wire_shape(Retyped) != wire_shape(A)
    assert wire_shape(Pick1) != wire_shape(Pick2)

    for shape in (wire_shape(A), wire_shape(B), wire_shape(), wire_shape(Pick2)):
        assert not _annotation_keys(shape) & FORBIDDEN


def test_the_version_line_keeps_the_form_tools_read():
    lines = Path(replay.__file__).read_text().splitlines()
    hits = [ln for ln in lines if re.fullmatch(r"WIRE_SCHEMA_VERSION: int = \d+", ln)]
    assert len(hits) == 1
    assert int(hits[0].rsplit("=", 1)[1]) == WIRE_SCHEMA_VERSION


def test_field_names_that_look_like_annotations_stay_in_the_shape():
    class Plain(BaseModel):
        a: str | None = None

    class Named(BaseModel):
        a: str | None = None
        title: str | None = Field(None, description="x")
        description: str | None = None

    shape = wire_shape(Named)
    assert {"title", "description"} <= set(shape["properties"])
    assert wire_shape(Named) != wire_shape(Plain)
    assert "description" not in shape["properties"]["title"]
    assert not _annotation_keys(shape) & FORBIDDEN


def test_the_shape_sees_defaults_requiredness_and_defaults_on_nested_fields():
    class Inner(BaseModel):
        x: int = 0

    class Base(BaseModel):
        inner: Inner = Inner(x=1)
        n: str | None = None

    class OtherDefault(BaseModel):
        inner: Inner = Inner(x=2)
        n: str | None = None

    class Required(BaseModel):
        inner: Inner = Inner(x=1)
        n: str | None

    class OtherPlainDefault(BaseModel):
        inner: Inner = Inner(x=1)
        n: str | None = "z"

    base = wire_shape(Base)
    assert base["properties"]["inner"]["default"] == {"x": 1}
    assert wire_shape(OtherDefault) != base
    assert wire_shape(Required) != base
    assert wire_shape(OtherPlainDefault) != base

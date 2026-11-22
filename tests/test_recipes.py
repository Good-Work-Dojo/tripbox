# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The Gmail filter recipe: built from the shipped sender list, so the document cannot drift."""

import re
from pathlib import Path

import pytest

from tripbox import lists
from tripbox.mail import recipes

DOC = Path(__file__).resolve().parent.parent / "docs" / "gmail-filters.md"


def _domains_in(query: str) -> list[str]:
    assert query.startswith("from:(") and query.endswith(")")
    return query[len("from:(") : -1].split(" OR ")


def test_every_filter_fits_gmails_limit_and_every_domain_is_in_exactly_one():
    domains = lists.trusted_sender_domains()
    queries = recipes.gmail_queries(domains)
    assert queries
    assert all(len(q) <= recipes.GMAIL_FILTER_MAX for q in queries)
    flat = [d for q in queries for d in _domains_in(q)]
    assert flat == list(domains)


def test_a_long_list_is_split_and_a_domain_too_long_is_refused():
    domains = [f"carrier-number-{i:03d}.example" for i in range(200)]
    queries = recipes.gmail_queries(domains)
    assert len(queries) > 1
    assert all(len(q) <= recipes.GMAIL_FILTER_MAX for q in queries)
    assert [d for q in queries for d in _domains_in(q)] == domains
    # each string is as full as the limit allows: the next domain would not have fitted
    for q, nxt in zip(queries, queries[1:], strict=False):
        first_of_next = _domains_in(nxt)[0]
        assert len(q) + len(" OR ") + len(first_of_next) > recipes.GMAIL_FILTER_MAX
    with pytest.raises(ValueError):
        recipes.gmail_queries(["a" * 2000 + ".example"])
    with pytest.raises(ValueError):
        recipes.gmail_queries(["one.example", "two.example"], limit=10)


def test_the_committed_recipe_is_what_the_generator_writes():
    assert DOC.read_bytes() == recipes.gmail_doc().encode("utf-8")
    assert recipes.gmail_doc().endswith("\n") and not recipes.gmail_doc().endswith("\n\n")


def test_the_recipe_names_only_example_addresses():
    text = recipes.gmail_doc()
    addresses = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
    assert addresses
    assert all(a.endswith("@example.com") for a in addresses)
    assert recipes.family_query() in text
    assert "trips@example.com" in text
    assert "TRIPBOX_MAIL_TRUST=gmail" in text

# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
import datetime as dt
from types import SimpleNamespace

from test_kitinerary_map import flight_obj

from tripbox.parse.anthropic_provider import AnthropicProvider
from tripbox.parse.models import WireExtraction, WireReservation
from tripbox.parse.pipeline import from_text, parse_message

NOW = dt.datetime(2026, 10, 24, tzinfo=dt.UTC)


class FakeMessages:
    def __init__(self, parsed):
        self.parsed, self.calls = parsed, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            parsed_output=self.parsed,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


def model_with(parsed):
    fake = FakeMessages(parsed)
    return AnthropicProvider(api_key="k", client=SimpleNamespace(messages=fake)), fake


def nm(subject="Delta receipt", text="Conf ABC123"):
    m = from_text(text, now=NOW, subject=subject)
    m.raw_for_kitinerary = b"raw eml"
    return m


def test_complete_kitinerary_result_skips_the_model():
    hx, fake = model_with(WireExtraction(document_type="other"))
    pm = parse_message(
        nm(), hx, runner=lambda data, suffix, ctx: [flight_obj("ALEX SAMPLE", "1A")]
    )
    assert pm.parser == "kitinerary" and fake.calls == []
    assert pm.extraction.reservations[0].kind == "flight"
    assert pm.kitinerary_objects and pm.usage is None


def test_empty_kitinerary_falls_back_to_the_model_without_hint():
    parsed = WireExtraction(
        document_type="booking",
        confidence=0.95,
        reservations=[WireReservation(kind="lodging", place="Hilton")],
    )
    hx, fake = model_with(parsed)
    pm = parse_message(nm(), hx, runner=lambda *a: [])
    assert pm.parser == "haiku" and pm.used_hint is False
    assert pm.extraction.reservations[0].kind == "lodging"
    assert pm.extraction.reservations[0].property_name == "Hilton"
    assert pm.usage.input_tokens == 10
    assert "deterministic parser" not in fake.calls[0]["messages"][0]["content"][-1]["text"]


def test_incomplete_kitinerary_passes_hint_to_the_model():
    obj = flight_obj("ALEX SAMPLE", "1A")
    del obj["reservationFor"]["departureTime"]
    hx, fake = model_with(WireExtraction(document_type="booking", confidence=0.9))
    pm = parse_message(nm(), hx, runner=lambda *a: [obj])
    assert pm.parser == "haiku" and pm.used_hint is True
    assert "deterministic parser" in fake.calls[0]["messages"][0]["content"][-1]["text"]


def test_subject_cancellation_overrides_booking():
    pm = parse_message(
        nm(subject="Your trip has been cancelled"),
        None,
        runner=lambda *a: [flight_obj("ALEX SAMPLE", "1A")],
    )
    assert pm.extraction.document_type == "cancellation"
    assert pm.extraction.reservations[0].status == "cancelled"


def test_no_model_and_incomplete_is_unparsed_not_failed():
    pm = parse_message(nm(), None, runner=lambda *a: [])
    assert pm.parser == "none" and pm.error is None


def test_text_input_uses_txt_suffix():
    seen = {}

    def runner(data, suffix, ctx):
        seen.update(data=data, suffix=suffix, ctx=ctx)
        return [flight_obj("X", None)]

    parse_message(from_text("DL 1234 MSP-PHL", now=NOW), None, runner=runner)
    assert (
        seen["suffix"] == ".txt"
        and seen["data"] == b"DL 1234 MSP-PHL"
        and seen["ctx"] == "2026-10-24"
    )


def test_cancelled_subject_leaves_an_explicitly_confirmed_booking_alone():
    """'Your flight was cancelled — you have been rebooked' carries the NEW flight, which the
    source marks confirmed; only unstated statuses take the subject's word."""
    ok = "http://schema.org/ReservationConfirmed"
    pm = parse_message(
        nm(subject="Your flight has been cancelled - you have been rebooked"),
        None,
        runner=lambda *a: [flight_obj("ALEX SAMPLE", "1A", status=ok)],
    )
    assert pm.extraction.document_type == "booking"
    assert pm.extraction.reservations[0].status == "confirmed"

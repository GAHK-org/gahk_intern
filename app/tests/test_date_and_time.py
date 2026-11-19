"""core.date_and_time: the date box plus 24-hour time box every date-and-time field uses."""

import datetime
import re
from pathlib import Path

import pytest
from django import forms
from django.utils import timezone

from core.date_and_time import DateAndTimeField, parse_clock
from events.forms import EventForm


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("19:30", datetime.time(19, 30)),
        ("19.30", datetime.time(19, 30)),
        ("19,30", datetime.time(19, 30)),
        ("1930", datetime.time(19, 30)),
        ("930", datetime.time(9, 30)),
        ("09:05", datetime.time(9, 5)),
        ("19", datetime.time(19, 0)),
        ("7", datetime.time(7, 0)),
        ("0", datetime.time(0, 0)),
        (" kl. 19.30 ", datetime.time(19, 30)),
        ("23:59", datetime.time(23, 59)),
    ],
)
def test_a_clock_is_read_as_24_hour_time(typed: str, expected: datetime.time) -> None:
    assert parse_clock(typed) == expected


@pytest.mark.parametrize("typed", ["24:00", "19:60", "193", "7 PM", "7:30 am", "19:3", "nu", ""])
def test_anything_else_is_not_a_clock(typed: str) -> None:
    assert parse_clock(typed) is None


class _Form(forms.Form):
    when = DateAndTimeField(label="Hvornår")
    maybe = DateAndTimeField(label="Måske", required=False)


def test_the_two_boxes_make_an_aware_copenhagen_time() -> None:
    form = _Form({"when_date": "2026-12-05", "when_time": "1930"})

    assert form.is_valid(), form.errors
    assert form.cleaned_data["when"] == datetime.datetime(2026, 12, 5, 18, 30, tzinfo=datetime.UTC)
    assert form.cleaned_data["maybe"] is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"when_date": "2026-12-05", "when_time": ""}, "Skriv også et klokkeslæt."),
        ({"when_date": "", "when_time": "19:30"}, "Vælg også en dato."),
        ({"when_date": "2026-12-05", "when_time": "7 PM"}, "Skriv klokkeslættet som fx 19:30."),
        ({"when_date": "5/12", "when_time": "19:30"}, "Vælg en gyldig dato."),
    ],
)
def test_a_half_filled_or_unreadable_pair_says_what_is_wrong(data: dict[str, str], message: str) -> None:
    form = _Form(data)

    assert not form.is_valid()
    assert form.errors["when"] == [message]


def test_an_optional_pair_says_which_half_is_missing_too() -> None:
    form = _Form({"when_date": "2026-12-05", "when_time": "19:30", "maybe_date": "2026-12-06"})

    assert form.errors["maybe"] == ["Skriv også et klokkeslæt."]


def test_a_time_skipped_by_summer_time_is_an_error_not_a_crash() -> None:
    form = _Form({"when_date": "2027-03-28", "when_time": "02:30"})

    assert not form.is_valid()
    assert "when" in form.errors


def test_it_renders_a_date_box_and_a_24_hour_text_box() -> None:
    evening = timezone.make_aware(datetime.datetime(2026, 12, 5, 19, 30, 42))
    html = str(_Form(initial={"when": evening})["when"])

    assert 'type="date" name="when_date" value="2026-12-05"' in html
    assert re.search(r'<input type="text" name="when_time" value="19:30"[^>]*data-clock', html)
    assert "datetime-local" not in html


def test_the_label_points_at_the_date_box() -> None:
    assert _Form()["when"].id_for_label == "id_when_0"


APP = Path(__file__).resolve().parent.parent


def test_no_native_time_picker_anywhere_in_the_app() -> None:
    """Every time a person sets is typed into a 24-hour box.

    `type=time` and `type=datetime-local` draw the clock in the device's regional format, so on a
    phone set to a US region they show AM/PM whatever the page says. A new one would bring that back
    for whoever happens to have such a phone, which nobody developing on a Danish Mac would see.
    """
    native = re.compile(r"""datetime-local|type=["']?time\b|input_type\s*=\s*["']time|SplitDateTimeWidget""")
    offenders = [
        f"{path.relative_to(APP)}:{number}"
        for path in [*APP.rglob("*.py"), *APP.rglob("*.html")]
        if not {"tests", "static", "staticfiles", "media", ".venv"} & set(path.relative_to(APP).parts)
        and path.relative_to(APP) != Path("core/date_and_time.py")  # it explains why there are none
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if native.search(line)
    ]

    assert offenders == []


def test_a_required_pair_marks_both_boxes_required_and_an_optional_one_neither() -> None:
    form = _Form()

    assert str(form["when"]).count(" required") == 2
    assert " required" not in str(form["maybe"])


@pytest.mark.django_db
def test_the_event_form_ties_the_end_and_the_svarfrist_to_the_start() -> None:
    """frontend/src/date-and-time.ts reads these: the end follows the start, the svarfrist precedes it."""
    form = EventForm()

    assert 'data-date-and-time="starts_at"' in str(form["starts_at"])
    assert 'data-after="starts_at"' in str(form["ends_at"])
    assert 'data-before="starts_at"' in str(form["rsvp_deadline_at"])

"""A date-and-time form field whose time is always 24-hour.

A native `<input type="datetime-local">` (or `type="time"`) draws its clock in the DEVICE's regional
format, not the page's: on a phone or Mac set to a US region a Danish page shows "07:30 PM", and no
`lang` attribute or Django setting changes that. So the date stays a native date input — it has no
clock in it — and the time is a plain text box this module reads itself, which is the only way to
promise "19:30" on every device.

The two halves post as `<name>_date` (YYYY-MM-DD, what a date input sends) and `<name>_time`.
"""

import datetime
import re
from typing import Any, cast

from django import forms
from django.core.exceptions import ValidationError
from django.forms.utils import from_current_timezone
from django.utils import timezone

# "19:30", "19.30", "19,30" (a Danish number pad's separator), "1930", "930", "19" and "7". Two-digit
# minutes only, so "193" is an error rather than a guess at 19:03 or 01:93.
CLOCK = re.compile(r"(\d{1,2})(?:[:.,]?(\d{2}))?")


def parse_clock(text: str) -> datetime.time | None:
    """The time typed into a clock box, or None when it is not one."""
    match = CLOCK.fullmatch(text.strip().removeprefix("kl.").strip())
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2] or 0)
    if hour > 23 or minute > 59:
        return None
    return datetime.time(hour, minute)


class ClockField(forms.TimeField):
    """A 24-hour time typed as text; see parse_clock for what it accepts."""

    def to_python(self, value: object) -> datetime.time | None:
        if value in self.empty_values:
            return None
        if isinstance(value, datetime.time):
            return value
        parsed = parse_clock(str(value))
        if parsed is None:
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        return parsed


class ClockInput(forms.TimeInput):
    """Text, not `type=time` (which is the device's 12- or 24-hour clock). `inputmode=numeric` gives
    a phone its number pad; "1930" is accepted for the colon that pad lacks, and
    frontend/src/date-and-time.ts tidies it to "19:30" when the box is left."""

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        super().__init__(
            format="%H:%M",
            attrs={
                "inputmode": "numeric",
                "autocomplete": "off",
                "placeholder": "tt:mm",
                "maxlength": 5,
                "size": 5,
                "aria-label": "Klokkeslæt",
                "data-clock": "",
                **(attrs or {}),
            },
        )


class DateAndTimeInput(forms.MultiWidget):
    """`after` / `before` name another date-and-time field of the same form that this one must come
    after (an end) or before (a deadline). frontend/src/date-and-time.ts uses it to fill in and move
    the end with the start, and to keep each calendar to the days allowed; the form still checks."""

    template_name = "core/date_and_time_input.html"

    def __init__(self, attrs: dict[str, Any] | None = None, *, after: str = "", before: str = "") -> None:
        widgets: dict[str, forms.Widget | type[forms.Widget]] = {
            # A date input still, which frontend/src/date-picker.ts turns into its Danish calendar. It
            # must be given exactly YYYY-MM-DD or it comes up blank (and an edit would then wipe the
            # date on save).
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "time": ClockInput(),
        }
        self.after = after
        self.before = before
        super().__init__(widgets, attrs)

    def get_context(self, name: str, value: object, attrs: dict[str, Any] | None) -> dict[str, Any]:
        context = super().get_context(name, value, attrs)
        context["widget"].update(after=self.after, before=self.before)
        return context

    def decompress(self, value: datetime.datetime | None) -> list[object]:
        if not value:
            return [None, None]
        local = timezone.localtime(value) if timezone.is_aware(value) else value
        return [local.date(), local.time().replace(second=0, microsecond=0)]

    def id_for_label(self, id_: str) -> str:
        # Django leaves a MultiWidget's <label> unattached; point it at the date, the first box.
        return f"{id_}_0" if id_ else id_  # sub-widget ids are numbered even when the names are not


class DateAndTimeField(forms.MultiValueField):
    """A local date and a 24-hour time, combined into an aware datetime in the current time zone."""

    widget = DateAndTimeInput
    default_error_messages = {
        "invalid_date": "Vælg en gyldig dato.",
        "invalid_time": "Skriv klokkeslættet som fx 19:30.",
        "missing_date": "Vælg også en dato.",
        "missing_time": "Skriv også et klokkeslæt.",
    }

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401 - Django's field kwargs
        messages = {**self.default_error_messages, **kwargs.get("error_messages", {})}
        # A required field's halves are required too, which is what puts `required` on both boxes
        # (and leaves "Ryd" out of its calendar). Their "incomplete" message is Django's word for
        # "this half is empty"; an optional field's half-filled pair is caught in compress().
        required = kwargs.get("required", True)
        fields = (
            forms.DateField(
                input_formats=["%Y-%m-%d"],
                required=required,
                error_messages={"invalid": messages["invalid_date"], "incomplete": messages["missing_date"]},
            ),
            ClockField(
                required=required,
                error_messages={"invalid": messages["invalid_time"], "incomplete": messages["missing_time"]},
            ),
        )
        # require_all_fields=False so a half-filled pair says which half is missing, instead of
        # Django's generic "Indtast en fuldstændig værdi".
        super().__init__(fields, require_all_fields=False, **kwargs)

    def compress(self, data_list: list[object]) -> datetime.datetime | None:
        if not data_list:
            return None
        day, clock = data_list
        if day is None and clock is None:
            if self.required:
                raise ValidationError(self.error_messages["required"], code="required")
            return None
        if day is None:
            raise ValidationError(self.error_messages["missing_date"], code="missing_date")
        if clock is None:
            raise ValidationError(self.error_messages["missing_time"], code="missing_time")
        # from_current_timezone, not make_aware: a time skipped by the switch to summer time (02:30 on
        # the last Sunday of March) becomes a form error rather than a crash.
        return from_current_timezone(
            datetime.datetime.combine(cast(datetime.date, day), cast(datetime.time, clock))
        )
